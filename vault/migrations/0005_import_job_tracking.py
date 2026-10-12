"""
Import job tracking and record-level ledger.

Adds three new tables for durable import tracking:
- ImportJob: persistent import operation record
- ImportRecord: per-source-record ledger entries
- ImportRecoveryLog: audit trail of recovery attempts

Additive migration — no changes to existing tables or data.
"""

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vault', '0004_alter_account_created_at_alter_account_updated_at_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ImportJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('job_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='Job ID')),
                ('source_name', models.CharField(max_length=1024, verbose_name='Source name')),
                ('source_path', models.TextField(blank=True, verbose_name='Source path')),
                ('source_size', models.BigIntegerField(blank=True, null=True, verbose_name='Source size (bytes)')),
                ('source_hash', models.CharField(blank=True, help_text='SHA-256 of the source file(s), for change detection.', max_length=64, verbose_name='Source hash')),
                ('import_format', models.CharField(default='auto', max_length=50, verbose_name='Import format')),
                ('options', models.JSONField(blank=True, default=dict, verbose_name='Import options')),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('paused', 'Paused'), ('completed', 'Completed'), ('completed_with_errors', 'Completed with errors'), ('failed', 'Failed'), ('cancelled', 'Cancelled'), ('interrupted', 'Interrupted')], db_index=True, default='pending', max_length=30, verbose_name='Status')),
                ('stage', models.CharField(choices=[('initializing', 'Initializing'), ('scanning', 'Scanning'), ('parsing', 'Parsing'), ('importing', 'Importing'), ('finalizing', 'Finalizing'), ('recovering', 'Recovering'), ('complete', 'Complete')], default='initializing', max_length=30, verbose_name='Stage')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, db_index=True, verbose_name='Created at')),
                ('started_at', models.DateTimeField(blank=True, null=True, verbose_name='Started at')),
                ('last_activity_at', models.DateTimeField(blank=True, null=True, verbose_name='Last activity at')),
                ('completed_at', models.DateTimeField(blank=True, null=True, verbose_name='Completed at')),
                ('total_discovered', models.BigIntegerField(default=0, verbose_name='Total discovered')),
                ('total_committed', models.BigIntegerField(default=0, verbose_name='Total committed')),
                ('total_skipped', models.BigIntegerField(default=0, verbose_name='Total skipped')),
                ('total_failed', models.BigIntegerField(default=0, verbose_name='Total failed')),
                ('last_checkpoint', models.JSONField(blank=True, default=dict, verbose_name='Last checkpoint')),
                ('recovery_attempts', models.IntegerField(default=0, verbose_name='Recovery attempts')),
                ('last_error', models.TextField(blank=True, verbose_name='Last error')),
                ('worker_id', models.CharField(blank=True, max_length=255, verbose_name='Worker ID')),
                ('lease_expires_at', models.DateTimeField(blank=True, null=True, verbose_name='Lease expires at')),
                ('importer_version', models.CharField(default='2.0', max_length=50, verbose_name='Importer version')),
            ],
            options={
                'verbose_name': 'Import Job',
                'verbose_name_plural': 'Import Jobs',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['status'], name='vault_importjob_status_idx'),
                    models.Index(fields=['-created_at'], name='vault_importjob_created_idx'),
                    models.Index(fields=['worker_id', 'lease_expires_at'], name='vault_importjob_lease_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ImportRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('import_job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='records', to='vault.importjob', verbose_name='Import job')),
                ('source_position', models.BigIntegerField(help_text='Line number or byte offset in the source.', verbose_name='Source position')),
                ('source_hash', models.CharField(help_text='SHA-256 of the source line, for change detection.', max_length=64, verbose_name='Source hash')),
                ('source_line', models.TextField(blank=True, help_text='Original source line, for debugging (truncated if long).', verbose_name='Source line')),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('committed', 'Committed'), ('skipped', 'Skipped'), ('failed', 'Failed')], db_index=True, default='pending', max_length=20, verbose_name='Status')),
                ('account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='import_records', to='vault.account', verbose_name='Account')),
                ('error_message', models.TextField(blank=True, verbose_name='Error message')),
                ('error_category', models.CharField(blank=True, max_length=50, verbose_name='Error category')),
                ('processed_at', models.DateTimeField(blank=True, null=True, verbose_name='Processed at')),
                ('idempotency_key', models.CharField(help_text='Deterministic key to prevent duplicate processing on retry.', max_length=128, unique=True, verbose_name='Idempotency key')),
            ],
            options={
                'verbose_name': 'Import Record',
                'verbose_name_plural': 'Import Records',
                'ordering': ['import_job', 'source_position'],
                'indexes': [
                    models.Index(fields=['import_job', 'status'], name='vault_importrec_job_status_idx'),
                    models.Index(fields=['import_job', 'source_position'], name='vault_importrec_job_pos_idx'),
                    models.Index(fields=['idempotency_key'], name='vault_importrec_idem_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ImportRecoveryLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('import_job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recovery_logs', to='vault.importjob', verbose_name='Import job')),
                ('attempt_number', models.IntegerField(verbose_name='Attempt number')),
                ('recovery_started_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='Recovery started at')),
                ('recovery_completed_at', models.DateTimeField(blank=True, null=True, verbose_name='Recovery completed at')),
                ('records_committed', models.BigIntegerField(default=0, verbose_name='Records committed')),
                ('records_pending', models.BigIntegerField(default=0, verbose_name='Records pending')),
                ('records_failed', models.BigIntegerField(default=0, verbose_name='Records failed')),
                ('reconciliation_details', models.JSONField(blank=True, default=dict, verbose_name='Reconciliation details')),
                ('outcome', models.CharField(choices=[('success', 'Success'), ('partial', 'Partial'), ('failed', 'Failed')], default='success', max_length=20, verbose_name='Outcome')),
                ('notes', models.TextField(blank=True, verbose_name='Notes')),
            ],
            options={
                'verbose_name': 'Import Recovery Log',
                'verbose_name_plural': 'Import Recovery Logs',
                'ordering': ['-recovery_started_at'],
            },
        ),
    ]
