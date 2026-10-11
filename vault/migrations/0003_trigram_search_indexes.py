"""
Trigram indexes for substring search on the reference tables.

Searches such as "contains this text" (ILIKE '%text%') cannot use a normal
B-tree index, so PostgreSQL reads the whole table for them. A GIN index on
pg_trgm trigrams lets it read only the matching rows. The expression matches
the one Django's icontains lookup generates, UPPER(name::text).

pg_trgm is a trusted extension, so the database owner can create it without
superuser rights.

Cost: these indexes slow inserts (about a third on the development box) and
take time to build on large tables. For a very large one-off load, drop them
first (DROP INDEX vault_url_name_trgm, ...) and recreate them afterwards with
the statements in FORWARD below.
"""

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


FORWARD = [
    "CREATE INDEX vault_url_name_trgm ON vault_url USING gin (UPPER(name::text) gin_trgm_ops)",
    "CREATE INDEX vault_email_name_trgm ON vault_email USING gin (UPPER(name::text) gin_trgm_ops)",
    "CREATE INDEX vault_user_name_trgm ON vault_user USING gin (UPPER(name::text) gin_trgm_ops)",
    "CREATE INDEX vault_pass_name_trgm ON vault_pass USING gin (UPPER(name::text) gin_trgm_ops)",
    "CREATE INDEX vault_ip_name_trgm ON vault_ip USING gin (UPPER(name::text) gin_trgm_ops)",
]

BACKWARD = [
    "DROP INDEX IF EXISTS vault_url_name_trgm",
    "DROP INDEX IF EXISTS vault_email_name_trgm",
    "DROP INDEX IF EXISTS vault_user_name_trgm",
    "DROP INDEX IF EXISTS vault_pass_name_trgm",
    "DROP INDEX IF EXISTS vault_ip_name_trgm",
]


class Migration(migrations.Migration):

    dependencies = [
        ('vault', '0002_canonical_values_and_record_fingerprint'),
    ]

    operations = [
        TrigramExtension(),
    ] + [
        migrations.RunSQL(sql, reverse_sql=rev)
        for sql, rev in zip(FORWARD, BACKWARD)
    ]
