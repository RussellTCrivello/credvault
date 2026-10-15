"""Bulk-import every line of every text file inside a folder (or a single file).

Uses the durable import engine to track every record with full auditability,
checkpointing, and recovery support.  Each import creates an ImportJob that
survives interruptions and can be resumed.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from vault.import_engine import SourceChangedError, durable_import_path
from vault.importer import search_indexes_suspended, stage_rows, worker_count


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
            help="Drop the trigram search indexes for the import and rebuild "
                 "them once at the end. Much faster for millions of lines.",
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

        for base in paths:
            if not (os.path.isdir(base) or os.path.isfile(base)):
                self.stderr.write(
                    self.style.ERROR(f"Not a directory or file: {base}")
                )
                return

        if not quiet:
            self.stdout.write(
                f"Durable import engine: batch size {stage_rows():,} rows; "
                f"{worker_count()} parse worker(s); "
                f"fast mode {'on' if fast else 'off'}."
            )

        total_created = 0
        total_skipped = 0
        total_failed = 0
        total_jobs = 0
        incomplete_jobs = 0

        # Use the durable engine with optional fast mode
        context = search_indexes_suspended() if fast else _null_context()
        with context:
            for base in paths:
                try:
                    job = durable_import_path(base, ext=ext)
                except SourceChangedError as exc:
                    raise CommandError(
                        f'{base}: {exc} Keep the source unchanged and '
                        're-run the command.'
                    ) from exc
                total_jobs += 1

                if not quiet:
                    throughput = job.throughput_per_second
                    elapsed = job.elapsed_seconds
                    line = (
                        f"{base}: job {job.job_id} — "
                        f"{job.total_committed:,} committed, "
                        f"{job.total_skipped:,} skipped, "
                        f"{job.total_failed:,} failed"
                    )
                    if elapsed > 0:
                        line += f" | {elapsed}s elapsed"
                        if throughput > 0:
                            line += f" @ {throughput:,} lines/s"
                    self.stdout.write(line)

                total_created += job.total_committed
                total_skipped += job.total_skipped
                total_failed += job.total_failed
                if job.status not in ('completed', 'completed_with_errors'):
                    incomplete_jobs += 1
                    self.stderr.write(
                        self.style.ERROR(
                            f"{base}: import job {job.job_id} ended "
                            f"'{job.status}'; inspect it on the import jobs "
                            'page before retrying.'
                        )
                    )

        if total_jobs == 0:
            self.stdout.write(
                self.style.WARNING("No paths were processed.")
            )
        elif incomplete_jobs:
            self.stderr.write(
                self.style.ERROR(
                    f"Done with errors. {incomplete_jobs} of "
                    f"{total_jobs} import job(s) did not complete. "
                    f"Imported {total_created} record(s); "
                    f"skipped {total_skipped} non-record line(s); "
                    f"failed {total_failed}."
                )
            )
            raise CommandError(
                f'{incomplete_jobs} of {total_jobs} import job(s) did not '
                'complete.'
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


class _null_context:
    """A no-op context manager for when fast mode is off."""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
