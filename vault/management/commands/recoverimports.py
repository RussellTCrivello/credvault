"""Recover import jobs abandoned by a crashed worker or restarted server.

``recoverimports`` lists the recoverable jobs; ``--run`` reconciles each
against the ledger and replays only the source positions that were never
committed (same code path as the Resume button). A job whose lease is still
live is never touched, and a source file that changed since the manifest was
taken is refused rather than replayed under old positions.
"""

from django.core.management.base import BaseCommand

from vault.import_engine import lines_factory_for_job, recover_job
from vault.models import ImportJob

ACTIVE = ('pending', 'processing', 'paused', 'interrupted', 'failed',
          'completed_with_errors')


class Command(BaseCommand):
    help = 'List (default) or recover (--run) abandoned durable import jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--run', action='store_true',
                            help='Recover every listed job instead of listing.')
        parser.add_argument('--job', help='Only this job id (UUID).')

    def handle(self, *args, **options):
        jobs = ImportJob.objects.filter(status__in=ACTIVE).order_by('pk')
        if options['job']:
            jobs = jobs.filter(job_id=options['job'])
        candidates = [job for job in jobs if job.can_resume()]
        if not candidates:
            self.stdout.write('No recoverable import jobs.')
            return
        for job in candidates:
            line = (
                f'{job.job_id} status={job.status} '
                f'committed={job.total_committed} skipped={job.total_skipped} '
                f'failed={job.total_failed} source={job.source_name}'
            )
            if not options['run']:
                self.stdout.write(line)
                continue
            log = recover_job(job, lines_iterable_factory=lines_factory_for_job(job))
            job.refresh_from_db()
            self.stdout.write(
                f'{line} -> recovery {log.outcome}; job now {job.status} '
                f'(committed={job.total_committed} skipped={job.total_skipped} '
                f'failed={job.total_failed})'
            )
