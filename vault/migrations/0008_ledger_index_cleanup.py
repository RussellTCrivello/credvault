"""Drop indexes that duplicate another index on the same column.

Measured on 1,000,000 source lines (880k accounts), each of these was an
exact duplicate or a prefix of an index that stays, and none had a reader:

* ``vault_importrec_idem_idx`` -- identical btree on ``idempotency_key`` to
  the unique constraint index (125 MB).
* ``vault_importrecord_idempotency_key_da77985c_like`` and
  ``vault_account_fingerprint_5bceda5e_like`` -- Django's ``varchar_pattern_ops``
  companions of two unique hex digest columns. They only serve ``LIKE
  'prefix%'``; the code only does equality / ``IN`` on these columns (125 MB
  and 110 MB).
* ``vault_importrecord_import_job_id_8be0a716`` -- the FK index, a strict
  prefix of the (import_job, source_position) and (import_job, status)
  indexes (6 MB).

Uniqueness is unaffected: the unique constraint indexes are untouched.
Rollback recreates every index (a full table scan each on large tables), so
take a backup first on a big ledger. ``DROP INDEX`` takes a short ACCESS
EXCLUSIVE lock; run during a quiet period.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vault', '0007_import_reliability_counters_and_manifest'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='importrecord',
            name='vault_importrec_idem_idx',
        ),
        migrations.AlterField(
            model_name='importrecord',
            name='import_job',
            field=models.ForeignKey(
                db_index=False, on_delete=models.deletion.CASCADE,
                related_name='records', to='vault.importjob',
                verbose_name='Import job',
            ),
        ),
        migrations.RunSQL(
            sql=[
                'DROP INDEX IF EXISTS vault_importrecord_idempotency_key_da77985c_like',
                'DROP INDEX IF EXISTS vault_account_fingerprint_5bceda5e_like',
            ],
            reverse_sql=[
                'CREATE INDEX IF NOT EXISTS vault_importrecord_idempotency_key_da77985c_like'
                ' ON vault_importrecord USING btree (idempotency_key varchar_pattern_ops)',
                'CREATE INDEX IF NOT EXISTS vault_account_fingerprint_5bceda5e_like'
                ' ON vault_account USING btree (fingerprint varchar_pattern_ops)',
            ],
        ),
    ]
