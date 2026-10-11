"""
Bulk-import every line of every text file inside one or more folders.

This is the recommended path for very large volumes: files are streamed from
disk in batches, so a 5 GB folder is imported with a few megabytes of memory
and one query per batch instead of one per line.

    python manage.py importfolder /path/to/folder
    python manage.py importfolder /a /b --ext txt --ext csv
    python manage.py importfolder /path/to/folder --dry-run --show-errors 10
    python manage.py importfolder /path/to/folder --fast
"""

import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from vault.importer import (
    DEFAULT_BATCH_SIZE, collect_files, import_paths, scan_file,
)
from vault.utils import TEXT_EXTENSIONS, looks_like_text


class Command(BaseCommand):
    help = (
        "Recursively read every text file in a folder (or several folders), "
        "parse each line, and store every line that meets the requirements. "
        "Already-known values are reused and duplicate records are skipped, "
        "so the command is safe to re-run. This is the fastest path for very "
        "large volumes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "paths",
            nargs='+',
            help="Folder (or file) to import. Folders are read recursively.",
        )
        parser.add_argument(
            "--ext",
            action="append",
            dest="extensions",
            help="Only files with this extension are read. Repeatable "
                 "(--ext txt --ext csv). Default: the text extensions the "
                 "web importer uses (.txt .text .csv .tsv .tab .log .dat "
                 ".list).",
        )
        parser.add_argument(
            "--all-extensions",
            action="store_true",
            help="Read every file in the folder regardless of extension.",
        )
        parser.add_argument(
            "--batch",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help="Lines held per batch (default: %(default)s). Raise it for "
                 "faster imports, lower it to use less memory.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would be imported without writing anything.",
        )
        parser.add_argument(
            "--show-errors",
            type=int,
            default=3,
            help="How many rejected lines to print as examples "
                 "(default: %(default)s).",
        )
        parser.add_argument(
            "--fast",
            action="store_true",
            help="On SQLite, switch to WAL + synchronous=NORMAL for the "
                 "duration of the import. Much faster, but a crash during "
                 "the run can lose the last transactions.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress per-file progress output.",
        )

    # ------------------------------------------------------------- helpers --

    def _apply_pragmas(self):
        if connection.vendor != 'sqlite':
            return False
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA cache_size=-65536")
        return True

    def _extensions(self, options):
        if options['all_extensions']:
            return None
        raw = options['extensions']
        if not raw:
            # Same set the browser folder pick uses, so "import this folder"
            # means the same thing from either side.
            return set(TEXT_EXTENSIONS)
        return {
            value if value.startswith('.') else f'.{value}'
            for value in raw
        }

    # -------------------------------------------------------------- handle --

    def handle(self, *args, **options):
        extensions = self._extensions(options)
        allow_any = options['all_extensions']
        batch = max(int(options['batch']), 100)
        quiet = options['quiet']
        show_errors = max(int(options['show_errors']), 0)

        for path in options['paths']:
            if not os.path.exists(path):
                raise CommandError(f"No such file or directory: {path}")

        files = collect_files(options['paths'], extensions, allow_any)
        if not files:
            self.stdout.write(self.style.WARNING(
                "No matching files found. Checked extensions: "
                + (", ".join(sorted(extensions)) if extensions
                   else "every file")
            ))
            return

        self.stdout.write(
            f"Found {len(files)} file(s) to read "
            f"({'dry run' if options['dry_run'] else 'importing'})."
        )

        if options['fast'] and self._apply_pragmas() and not quiet:
            self.stdout.write("SQLite tuned for this run: WAL, "
                              "synchronous=NORMAL, 64 MB cache.")

        started = time.perf_counter()

        if options['dry_run']:
            self._dry_run(files, show_errors, quiet)
            return

        def on_file(filepath, stats, error):
            if error is not None:
                self.stderr.write(self.style.ERROR(
                    f"{filepath}: could not be read ({error})"
                ))
                return
            if not quiet:
                self.stdout.write(
                    f"{os.path.basename(filepath)}: {stats.lines} lines, "
                    f"{stats.prepared} new relationships "
                    f"({stats.skipped} skipped)"
                )

        stats = import_paths(
            files, extensions, allow_any,
            on_file=on_file, batch_size=batch,
        )
        elapsed = time.perf_counter() - started
        rate = stats.lines / elapsed if elapsed else 0

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created {stats.created} record(s) from {stats.files} "
            f"file(s) in {elapsed:.1f}s ({rate:,.0f} lines/s)."
        ))
        if stats.prepared - stats.created:
            self.stdout.write(
                f"{stats.prepared - stats.created} line(s) matched records "
                f"that were already stored, so they were reused."
            )
        if stats.skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"{stats.skipped} line(s) were not valid records."
                )
            )

    def _dry_run(self, files, show_errors, quiet):
        total = valid = invalid = unreadable = 0
        examples = []
        for filepath in files:
            with open(filepath, 'rb') as handle:
                head = handle.read(1024)
                handle.seek(0)
                if not looks_like_text(head):
                    unreadable += 1
                    if not quiet:
                        self.stdout.write(
                            f"{os.path.basename(filepath)}: skipped "
                            f"(does not look like text)"
                        )
                    continue
            lines, good, bad, sample = scan_file(filepath, show_errors)
            total += lines
            valid += good
            invalid += bad
            for example in sample:
                if len(examples) < show_errors:
                    examples.append((filepath, example))
            if not quiet:
                self.stdout.write(
                    f"{os.path.basename(filepath)}: {lines} lines, "
                    f"{good} would be stored, {bad} would be skipped"
                )

        self.stdout.write(self.style.SUCCESS(
            f"Dry run. {total:,} line(s) in {len(files)} file(s): "
            f"{valid:,} would be stored, {invalid:,} would be skipped"
            + (f", {unreadable} file(s) unreadable" if unreadable else "")
            + "."
        ))
        if examples:
            self.stdout.write("First rejected lines:")
            for filepath, example in examples:
                self.stdout.write(f"  {os.path.basename(filepath)}: "
                                  f"{example!r}")
