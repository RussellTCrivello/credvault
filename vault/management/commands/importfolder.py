"""Bulk-import every line of every text file inside a folder (or a single file).

Uses the durable import engine to track every record with full auditability,
checkpointing, and recovery support. Each import creates an ImportJob that
survives interruptions and can be resumed.
"""

import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connections

from vault.import_engine import SourceChangedError, durable_import_path
from vault.importer import (
    collect_files_from_path,
    search_indexes_suspended,
    stage_rows,
    worker_count,
)

MAX_PARALLEL_FILES = 8


class Command(BaseCommand):
    help = (
        "Recursively read every text file in one or more folders (or single "
        "files), parse each line, and store valid records. Uses the durable "
        "import engine with full auditability and recovery support."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "paths",
            nargs="+",
            help="One or more directory or file paths containing the "
                 "records to import.",
        )
        parser.add_argument(
            "--ext",
            default="txt",
            help="Only files with this extension are read (default: txt). "
                 "Use '*' to read every file regardless of extension.",
        )
        parser.add_argument(
            "--fast",
            action="store_true",
            help="Drop the non-unique search indexes for the import and "
                 "rebuild them once at the end. This may slow interactive "
                 "searches while the import runs.",
        )
        parser.add_argument(
            "--parallel-files",
            type=int,
            default=1,
            metavar="N",
            help=(
                "Process up to N independent files concurrently (1-8). "
                "Each file gets its own durable import job; parser workers "
                "are set to one per file to keep the total concurrency bounded."
            ),
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress per-file progress output.",
        )

    def handle(self, *args, **options):
        paths = options["paths"]
        ext = options["ext"]
        fast = options["fast"]
        quiet = options["quiet"]
        parallel_files = options["parallel_files"]

        if not 1 <= parallel_files <= MAX_PARALLEL_FILES:
            raise CommandError(
                f"--parallel-files must be between 1 and "
                f"{MAX_PARALLEL_FILES}."
            )

        for base in paths:
            if not (os.path.isdir(base) or os.path.isfile(base)):
                self.stderr.write(
                    self.style.ERROR(f"Not a directory or file: {base}")
                )
                raise CommandError(f"Not a directory or file: {base}")

        if not quiet:
            self.stdout.write(
                f"Durable import engine: batch size {stage_rows():,} rows; "
                f"{worker_count()} parse worker(s); "
                f"file concurrency {parallel_files}; "
                f"fast mode {'on' if fast else 'off'}."
            )

        # Preserve the existing one-job-per-input-path contract by default.
        # The opt-in file mode expands directories into sorted, independent
        # file jobs, so each file retains its own manifest/checkpoint/recovery
        # identity and source positions remain deterministic within that file.
        context = search_indexes_suspended() if fast else _null_context()
        with context:
            if parallel_files == 1:
                sources = [(base, ext) for base in paths]
                jobs, errors = self._run_sequential(sources)
            else:
                sources = self._expand_file_sources(paths, ext)
                if len(sources) > 1:
                    jobs, errors = self._run_parallel(
                        sources, parallel_files,
                    )
                else:
                    # Do not disable the normal per-file parser pool when
                    # there is no independent file work to schedule.
                    jobs, errors = self._run_sequential(sources)

        total_created = 0
        total_skipped = 0
        total_failed = 0
        incomplete_jobs = 0

        for (source, _source_ext), job in zip(sources, jobs):
            if job is None:
                continue
            total_created += job.total_committed
            total_skipped += job.total_skipped
            total_failed += job.total_failed
            if not quiet:
                self.stdout.write(
                    f"{source}: job {job.job_id} — "
                    f"{job.total_committed} records committed, "
                    f"{job.total_skipped} skipped, "
                    f"{job.total_failed} failed"
                )
            if job.status not in ('completed', 'completed_with_errors'):
                incomplete_jobs += 1
                self.stderr.write(
                    self.style.ERROR(
                        f"{source}: import job {job.job_id} ended "
                        f"'{job.status}'; inspect it on the import jobs "
                        'page before retrying.'
                    )
                )

        total_jobs = sum(job is not None for job in jobs)
        if total_jobs == 0 and not errors:
            self.stdout.write(self.style.WARNING("No paths were processed."))
        elif incomplete_jobs or errors:
            for source, exc in errors:
                self.stderr.write(self.style.ERROR(
                    f"{source}: import failed ({type(exc).__name__})."
                ))
            self.stderr.write(
                self.style.ERROR(
                    f"Done with errors. {incomplete_jobs + len(errors)} of "
                    f"{len(sources)} import job(s) did not complete. "
                    f"Imported {total_created} record(s); "
                    f"skipped {total_skipped} non-record line(s); "
                    f"failed {total_failed}."
                )
            )
            raise CommandError(
                f'{incomplete_jobs + len(errors)} of {len(sources)} '
                'import job(s) did not complete.'
            )
        else:
            summary = (
                f"Done. {total_jobs} import job(s) completed. "
                f"Imported {total_created} record(s); "
                f"skipped {total_skipped} non-record line(s)."
            )
            if total_failed:
                summary += f" Failed {total_failed}."
            self.stdout.write(self.style.SUCCESS(summary))

    @staticmethod
    def _expand_file_sources(paths, ext):
        """Expand directories in deterministic order for file-level jobs."""
        sources = []
        for base in paths:
            if os.path.isdir(base):
                files = collect_files_from_path(base, ext=ext)
                if files:
                    sources.extend((filepath, ext) for filepath in files)
                else:
                    # Keep an empty directory observable as one zero-line job,
                    # matching the existing path-import behavior.
                    sources.append((base, ext))
            else:
                # A directly supplied file is imported regardless of its
                # extension, as in durable_import_path's existing contract.
                sources.append((base, ext))
        return sources

    def _run_sequential(self, sources):
        jobs = []
        for path, ext in sources:
            try:
                jobs.append(durable_import_path(path, ext=ext))
            except SourceChangedError as exc:
                raise CommandError(
                    f'{path}: {exc} Keep the source unchanged and '
                    're-run the command.'
                ) from exc
        return jobs, []

    def _run_parallel(self, sources, parallel_files):
        """Run independent file jobs with a bounded submission queue."""
        jobs = [None] * len(sources)
        errors = []

        def run_source(path, source_ext):
            # Django connections are thread-local; close stale wrappers before
            # and after work so worker threads do not leak connections.
            close_old_connections()
            try:
                # One parser per file worker avoids nested worker pools and
                # bounds total application concurrency by parallel_files.
                return durable_import_path(
                    path,
                    ext=source_ext,
                    workers=1,
                )
            finally:
                # ``close_old_connections`` only closes expired/unusable
                # wrappers; executor threads otherwise keep healthy database
                # sessions alive after they return to the pool.
                connections.close_all()
                close_old_connections()

        source_iterator = iter(enumerate(sources))
        with ThreadPoolExecutor(max_workers=parallel_files) as executor:
            pending = {}

            def submit_next():
                try:
                    index, (path, source_ext) = next(source_iterator)
                except StopIteration:
                    return False
                future = executor.submit(run_source, path, source_ext)
                pending[future] = (index, path)
                return True

            # Keep at most one task per worker in flight. A directory with
            # many files must not turn into an unbounded executor work queue.
            for _ in range(min(parallel_files, len(sources))):
                submit_next()

            while pending:
                completed, _not_done = wait(
                    pending, return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    index, path = pending.pop(future)
                    try:
                        jobs[index] = future.result()
                    except Exception as exc:
                        errors.append((path, exc))
                    submit_next()
        return jobs, errors


class _null_context:
    """A no-op context manager for when fast mode is off."""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
