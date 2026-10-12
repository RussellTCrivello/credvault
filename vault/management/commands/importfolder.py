"""Bulk-import every line of every text file inside a folder (or a single file)."""

import os

from django.core.management.base import BaseCommand

from vault.importer import import_path, stage_rows, worker_count


class Command(BaseCommand):
    help = (
        "Recursively read every text file in one or more folders (or single "
        "files), parse each line, and store valid records. This is the "
        "fastest path for very large volumes."
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
                f"Batch size {stage_rows():,} rows; "
                f"{worker_count()} parse worker(s); "
                f"fast mode {'on' if fast else 'off'}."
            )

        def _report(filepath, created, skipped, running_total):
            if not quiet:
                self.stdout.write(
                    f"{filepath}: +{created} records, {skipped} skipped "
                    f"(running total +{running_total})"
                )

        total_created = 0
        total_skipped = 0
        for base in paths:
            created_base, skipped_base, count = import_path(
                base, ext=ext, on_file=_report, fast_mode=fast
            )
            if count == 0:
                self.stdout.write(
                    self.style.WARNING(f"No matching files found: {base}")
                )
            total_created += created_base
            total_skipped += skipped_base

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Imported {total_created} record(s); "
                f"skipped {total_skipped} non-record line(s)."
            )
        )
