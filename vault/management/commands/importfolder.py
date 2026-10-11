"""Bulk-import every line of every text file inside a folder (or a single file)."""

import os
import sys

from django.core.management.base import BaseCommand

from vault.import_jobs import ImportJob
from vault.importer import collect_files_from_path, file_size, import_path


def format_bytes(count):
    size = float(count or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class LiveBar:
    """
    Terminal progress line driven by the same job object the web screen polls.

    It forwards the importer's tracker calls to the job and repaints the line
    every ``every`` bytes, so a single 20 GB file still shows a moving,
    measured percentage rather than one jump from 0% to 100%.
    """

    def __init__(self, job, stream=sys.stdout, enabled=True,
                 every=16 * 1024 * 1024):
        self.job = job
        self.stream = stream
        self.enabled = enabled
        self.every = every
        self._since_paint = 0
        self._file = 0
        self._files_total = 0

    def begin_file(self, name, index, total):
        self._file = index
        self._files_total = total
        self.paint()

    def file_bytes(self, count):
        self.job.file_bytes(count)
        self._since_paint += count
        if self.enabled and self._since_paint >= self.every:
            self._since_paint = 0
            self.paint()

    def note(self, **counts):
        self.job.note(**counts)

    def file_error(self, path, message):
        self.job.file_error(path, message)

    def stopped(self):
        return self.job.stopped()

    def paint(self):
        if not self.enabled:
            return
        summary = self.job.snapshot(include_files=False)
        percent = summary['percent'] if summary['percent'] is not None else 0.0
        width = 24
        filled = int(round(percent / 100 * width))
        bar = '#' * filled + '.' * (width - filled)
        self.stream.write(
            f"\r  [{bar}] {percent:5.1f}%  file {self._file}/{self._files_total}"
            f"  +{summary['created']:,} records"
            f"  {format_bytes(summary['bytes_done'])}"
            f"/{format_bytes(summary['bytes_total'])}"
        )
        self.stream.flush()

    def finish(self):
        if self.enabled:
            self.stream.write("\n")


class Command(BaseCommand):
    help = (
        "Recursively read every text file in a folder (or a single file), "
        "parse each line, and store valid records. This is the fastest path "
        "for very large volumes, and it reports the same byte-accurate "
        "progress as the web import: the total size of the matching files is "
        "measured up front, then each file reports the bytes it has read."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            help="Directory or file path containing the records to import.",
        )
        parser.add_argument(
            "--ext",
            default="txt",
            help="Only files with this extension are read (default: txt). "
                 "Use '*' to read every file regardless of extension.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress progress output.",
        )
        parser.add_argument(
            "--no-bar",
            action="store_true",
            help="Print one line per file instead of a running percentage.",
        )

    def handle(self, *args, **options):
        base = options["path"]
        ext = options["ext"]
        quiet = options["quiet"]
        bar = not (quiet or options["no_bar"])

        if not (os.path.isdir(base) or os.path.isfile(base)):
            self.stderr.write(
                self.style.ERROR(f"Not a directory or file: {base}")
            )
            return

        found = {'files': 0, 'bytes': 0}

        def on_found(count, total_bytes):
            found['files'] = count
            found['bytes'] = total_bytes
            if bar and count > 1:
                self.stdout.write(
                    f"\r  found {count} file(s), {format_bytes(total_bytes)}",
                    ending="",
                )

        files = collect_files_from_path(base, ext=ext, on_found=on_found)
        if bar and len(files) > 1:
            self.stdout.write("\r" + " " * 48 + "\r", ending="")

        if not files:
            self.stdout.write(self.style.WARNING("No matching files found."))
            return

        total_bytes = found['bytes'] or sum(
            file_size(path) or 0 for path in files
        )
        job = ImportJob(
            'console', label=base, source_kind='console',
            register_files=False, total_bytes=total_bytes,
        )
        tracker = LiveBar(job, stream=self.stdout, enabled=bar)

        def on_file(filepath, stats, running_total, index, files_total):
            if not bar and not quiet:
                self.stdout.write(
                    f"{filepath}: +{stats.created} records, "
                    f"{stats.skipped} skipped, {stats.duplicates} already "
                    f"stored (running total +{running_total})"
                )

        total_created, total_skipped, _count = import_path(
            base, ext=ext, on_file=on_file, tracker=tracker, files=files,
            on_start=tracker.begin_file,
        )
        tracker.finish()

        summary = job.snapshot(include_files=False)
        elapsed = summary['elapsed_seconds'] or 0.001
        self.stdout.write(self.style.SUCCESS(
            f"Done. Read {summary['lines']} line(s) from {len(files)} file(s) "
            f"({format_bytes(total_bytes)}) in {elapsed:.1f}s: imported "
            f"{total_created} record(s), skipped {total_skipped} non-record "
            f"line(s), reused {summary['duplicates']} already stored."
        ))
