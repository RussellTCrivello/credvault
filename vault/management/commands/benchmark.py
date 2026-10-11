"""
Measure import and search speed on this machine.

    python manage.py benchmark --rows 1000000 --import-mode fast
    python manage.py benchmark --file /data/records.txt --import-mode maximum

The import runs with the chosen import mode, then each search a user runs is
timed through the real web views. The command refuses to run on a database that
already holds records, so the numbers measure this data alone. Use --force only
on a scratch database.
"""

import os
import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from vault.benchmarking import (
    benchmark_client, database_vendor, run_queries, sample_values,
    temporary_directory, write_synthetic_file,
)
from vault.importer import collect_files_from_path, import_path
from vault.models import Account, PerformanceSettings
from vault.performance import (
    IMPORT_PROFILES, QUERY_PROFILES, import_profile, query_profile,
)


class Command(BaseCommand):
    help = (
        "Import records and time the searches on this machine. Run it on an "
        "empty scratch database."
    )

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group()
        source.add_argument(
            "--rows", type=int, default=1_000_000,
            help="Generate this many synthetic record lines (default: 1000000).",
        )
        source.add_argument(
            "--file", dest="file_path",
            help="Import this existing file or folder instead of generating data.",
        )
        parser.add_argument(
            "--import-mode", choices=sorted(IMPORT_PROFILES), default=None,
            help="Import speed (default: the mode saved on the Settings page).",
        )
        parser.add_argument(
            "--query-mode", choices=sorted(QUERY_PROFILES), default=None,
            help="Search speed (default: the mode saved on the Settings page).",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Run even when the database already holds records.",
        )

    def handle(self, *args, **options):
        if Account.objects.exists() and not options["force"]:
            raise CommandError(
                "The database already holds records. Benchmark on an empty "
                "scratch database, or pass --force to run anyway."
            )

        import_profile_obj = import_profile(options["import_mode"])
        query_profile_obj = (
            QUERY_PROFILES[options["query_mode"]]
            if options["query_mode"] else None
        )
        vendor = database_vendor()
        self.stdout.write(
            f"Database: {vendor}. Import mode: {import_profile_obj.key}. "
            f"Parsing processes: {import_profile_obj.parse_workers}."
        )

        if options["file_path"]:
            return self._run(options["file_path"], import_profile_obj,
                             query_profile_obj, vendor)

        with temporary_directory() as directory:
            path = os.path.join(directory, f"benchmark-{options['rows']}.txt")
            started = time.perf_counter()
            write_synthetic_file(path, options["rows"])
            self.stdout.write(
                f"Generated {options['rows']:,} lines in "
                f"{time.perf_counter() - started:.1f} s."
            )
            self._run(path, import_profile_obj, query_profile_obj, vendor)

    def _run(self, target, import_profile_obj, query_profile_obj, vendor):
        files = collect_files_from_path(target, ext="*")
        size = sum(os.path.getsize(path) for path in files)

        started = time.perf_counter()
        created, skipped, _count = import_path(
            target, ext="*", profile=import_profile_obj,
        )
        elapsed = time.perf_counter() - started
        rate = created / elapsed if elapsed else 0.0
        self.stdout.write(self.style.SUCCESS(
            f"Import: {created:,} records from {size / 1_048_576:,.0f} MB "
            f"({skipped:,} lines skipped) in {elapsed:,.1f} s = "
            f"{rate:,.0f} records/s [{import_profile_obj.key}]."
        ))

        query_profile_key = query_profile_obj.key if query_profile_obj else None
        self._time_queries(vendor, query_profile_key)

    def _time_queries(self, vendor, query_profile_key):
        user_model = get_user_model()
        administrator, _ = user_model.objects.get_or_create(
            username="benchmark-admin",
            defaults={"is_staff": True},
        )
        client = benchmark_client(administrator)
        values = sample_values()

        stored = PerformanceSettings.load()
        original_query_mode = stored.query_mode
        try:
            if query_profile_key:
                stored.query_mode = query_profile_key
                stored.save(update_fields=["query_mode"])
            mode = query_profile().key
            self.stdout.write(f"Searches [query mode: {mode}]:")
            with override_settings(ALLOWED_HOSTS=["*"]):
                for label, status, elapsed in run_queries(client, values, ""):
                    flag = "" if status == 200 else f"  (HTTP {status})"
                    self.stdout.write(f"  {label:<30} {elapsed:>10.1f} ms{flag}")
        finally:
            stored.query_mode = original_query_mode
            stored.save(update_fields=["query_mode"])
