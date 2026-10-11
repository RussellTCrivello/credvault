"""Bulk-import every line of every text file inside a folder (or a single file)."""

import os

from django.core.management.base import BaseCommand

from vault.importer import collect_files_from_path, import_path


class Command(BaseCommand):
    help = (
        "Recursively read every text file in a folder (or a single file), "
        "parse each line, and store valid records. This is the fastest path "
        "for very large volumes."
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
            help="Suppress per-file progress output.",
        )

    def handle(self, *args, **options):
        base = options["path"]
        ext = options["ext"]
        quiet = options["quiet"]

        if not (os.path.isdir(base) or os.path.isfile(base)):
            self.stderr.write(
                self.style.ERROR(f"Not a directory or file: {base}")
            )
            return

        files = collect_files_from_path(base, ext=ext)
        if not files:
            self.stdout.write(
                self.style.WARNING("No matching files found.")
            )
            return

        def _report(filepath, created, skipped, running_total):
            if not quiet:
                self.stdout.write(
                    f"{filepath}: +{created} records, {skipped} skipped "
                    f"(running total +{running_total})"
                )

        total_created, total_skipped, _count = import_path(
            base, ext=ext, on_file=_report
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Imported {total_created} record(s); "
                f"skipped {total_skipped} non-record line(s)."
            )
        )
