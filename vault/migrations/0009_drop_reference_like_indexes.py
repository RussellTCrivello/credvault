"""Drop the five ``varchar_pattern_ops`` companions of the reference ``name`` columns.

Django creates ``<table>_name_<hash>_like`` next to every unique/indexed
``CharField`` so that ``LIKE 'prefix%'`` can use a btree. In this application:

* equality, ``IN`` and uniqueness use the unique ``name`` index;
* case-insensitive exact search uses ``lower(name)`` (``*_name_lower_idx``);
* the domain prefix scan uses ``lower(name) COLLATE "C"`` (migration 0006);
* substring search uses the ``upper(name)`` trigram GIN indexes (migration 0003).

None of them reads the ``_like`` index. Measured after loading 5.5 M accounts
(``docs/BENCHMARK_RESULTS.md`` section 12): scans 0 on email/user/ip/url and
25,359 on the password index (equality probes the planner could equally serve
from ``name_key``). Dropping them saves 651 MB at 7.4 M accounts (~7 % of the
database) and 3 % of the WAL of a 440k-account import; the import rate was
+3.7 % in an unreplicated single run (within noise), so the case for the
migration is storage and write amplification, not speed.

Also dropped: the two single-column ledger ``status`` indexes (btree and its
``_like`` companion, 43 MB each at 6.2 M ledger rows, zero scans). Every status
query in the code is scoped to one import job and is served by the
``(import_job, status)`` index, which stays.

The unique constraints are untouched. Rollback recreates the five indexes with
a table scan each, so take a backup first on a large database. ``DROP INDEX``
takes a short ACCESS EXCLUSIVE lock; run during a quiet period.
"""

from django.db import migrations, models

_INDEXES = (
    ('vault_url_name_7d57bf4b_like', 'vault_url'),
    ('vault_email_name_1c4a6f40_like', 'vault_email'),
    ('vault_user_name_34073d8b_like', 'vault_user'),
    ('vault_pass_name_2ae36d82_like', 'vault_pass'),
    ('vault_ip_name_09e37067_like', 'vault_ip'),
)


class Migration(migrations.Migration):

    dependencies = [
        ('vault', '0008_ledger_index_cleanup'),
    ]

    operations = [
        migrations.AlterField(
            model_name='importrecord',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'), ('processing', 'Processing'),
                    ('committed', 'Committed'), ('skipped', 'Skipped'),
                    ('failed', 'Failed'),
                ],
                default='pending', max_length=20, verbose_name='Status',
            ),
        ),
        migrations.RunSQL(
            sql=[f'DROP INDEX IF EXISTS {name}' for name, _ in _INDEXES],
            reverse_sql=[
                f'CREATE INDEX IF NOT EXISTS {name} ON {table}'
                ' USING btree (name varchar_pattern_ops)'
                for name, table in _INDEXES
            ],
        ),
    ]
