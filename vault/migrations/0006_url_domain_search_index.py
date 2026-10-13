"""
Byte-order (COLLATE "C") functional index for domain prefix searches.

``views._match_url_ids`` answers "every URL on this domain" with B-tree
range scans of the form::

    lower(name) >= 'https://example.com/'
    AND lower(name) < 'https://example.com/' || <max code point>

That range means exactly "starts with the prefix" only under byte
ordering.  In a locale collation -- the default on clusters initialized
by the Windows installer, for instance -- the upper bound does not
reliably sort above ordinary text, so the scan silently matches nothing
and domain searches come back empty.  This index backs the COLLATE "C"
comparisons the query now makes, so domain search behaves (and stays
indexed) whichever collation the database itself was created with.

Like the trigram indexes (migration 0003) this is plain RunSQL: Django's
index declarations cannot express a per-expression collation.  The index
is additive -- the existing LOWER(name) index from migration 0002 keeps
serving exact-match lookups.
"""

from django.db import migrations


FORWARD = (
    'CREATE INDEX IF NOT EXISTS vault_url_name_lower_c_idx'
    ' ON vault_url (lower(name) COLLATE "C")'
)

BACKWARD = 'DROP INDEX IF EXISTS vault_url_name_lower_c_idx'


class Migration(migrations.Migration):

    dependencies = [
        ('vault', '0005_import_job_tracking'),
    ]

    operations = [
        migrations.RunSQL(FORWARD, reverse_sql=BACKWARD),
    ]
