"""
Idempotent relationship storage.

The UNIQUE index below is the reason a bulk import can be both *deduplicated*
and *fast*: the database rejects the repeated relationship, so the importer
never has to read the existing table into memory to compare against it.

It is written as raw SQL because the relationship columns are nullable and a
plain ``UNIQUE`` constraint treats every NULL as distinct -- which would let
the same record be stored twice as soon as the IP (or the unused email /
username side) is NULL. COALESCE folds the NULLs to 0 so the index is
NULL-aware.
"""

from django.db import migrations


TABLE = 'vault_account'
INDEX_NAME = 'vault_account_relationship_unique'

# Any duplicate that predates this migration is folded into its oldest row so
# the unique index can always be built.
DEDUPE_SQL = f"""
DELETE FROM {TABLE}
WHERE id NOT IN (
    SELECT MIN(id) FROM {TABLE}
    GROUP BY url_id, account_type,
             COALESCE(email_id, 0), COALESCE(user_id, 0),
             password_id, COALESCE(ip_id, 0)
);
"""

CREATE_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE} (
    url_id,
    account_type,
    COALESCE(email_id, 0),
    COALESCE(user_id, 0),
    password_id,
    COALESCE(ip_id, 0)
);
"""

DROP_SQL = f"DROP INDEX IF EXISTS {INDEX_NAME};"


class Migration(migrations.Migration):
    dependencies = [
        ('vault', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(sql=DEDUPE_SQL, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
    ]
