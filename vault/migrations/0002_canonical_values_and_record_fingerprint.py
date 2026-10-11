"""
Store every value in canonical form and make "never twice" a database rule.

Three things happen, in this order:

1. ``Account.fingerprint`` is added (nullable, so it can be backfilled).
2. Every URL / email / username / password / IP already stored is rewritten
   in canonical form and the rows that then turn out to be the same value
   are merged into one (the records pointing at them are re-attached first).
   Records that become identical after the merge are deleted.  Values that
   do not match their format are left exactly as they are -- a migration
   must never destroy data it does not understand.
3. The fingerprints are computed and the column is made unique, so from now
   on the same record cannot be stored twice even on the bulk import path.
"""

import hashlib

from django.db import migrations, models


REFERENCE_KINDS = (
    ('Url', 'url', 'url'),
    ('Email', 'email', 'email'),
    ('User', 'user', 'user'),
    ('Pass', 'pass', 'password'),
    ('Ip', 'ip', 'ip'),
)


def _record_fingerprint(
    url_id, account_type, email_id, user_id, password_id, ip_id
):
    """
    Copy of ``vault.models.record_fingerprint`` at the time this migration
    was written.  Migrations keep their own copy so that later changes to
    the application cannot alter what already-migrated rows mean.
    """
    payload = (
        f'{url_id or ""}|{account_type or ""}|{email_id or ""}'
        f'|{user_id or ""}|{password_id or ""}|{ip_id or ""}'
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _canonicalize(kind, value):
    from vault.validators import CANONICALIZERS
    return CANONICALIZERS[kind](value)


def canonicalize_reference_values(apps, schema_editor):
    """Rewrite stored values in canonical form and merge equal values."""
    Account = apps.get_model('vault', 'Account')

    for model_name, kind, field in REFERENCE_KINDS:
        model = apps.get_model('vault', model_name)

        groups = {}
        for row in model.objects.order_by('id').values_list('id', 'name'):
            canonical = _canonicalize(kind, row[1])
            if canonical is None:
                canonical = row[1]  # unknown data is kept as it is
            groups.setdefault(canonical, []).append(row)

        for canonical, rows in groups.items():
            # Prefer the row that already holds the canonical spelling so
            # that its construction date is the one that survives.
            keeper = next(
                (row for row in rows if row[1] == canonical), rows[0]
            )
            for row_id, _name in rows:
                if row_id == keeper[0]:
                    continue
                Account.objects.filter(**{f'{field}_id': row_id}).update(
                    **{f'{field}_id': keeper[0]}
                )
                model.objects.filter(pk=row_id).delete()
            if keeper[1] != canonical:
                model.objects.filter(pk=keeper[0]).update(name=canonical)


def deduplicate_and_fingerprint_accounts(apps, schema_editor):
    """Keep one row per distinct relationship and hash it."""
    Account = apps.get_model('vault', 'Account')

    seen = set()
    duplicates = []
    fingerprints = []
    for row in Account.objects.order_by('id').iterator():
        fingerprint = _record_fingerprint(
            row.url_id, row.account_type, row.email_id,
            row.user_id, row.password_id, row.ip_id,
        )
        if fingerprint in seen:
            duplicates.append(row.id)
            continue
        seen.add(fingerprint)
        row.fingerprint = fingerprint
        fingerprints.append(row)

    for start in range(0, len(duplicates), 500):
        Account.objects.filter(
            pk__in=duplicates[start:start + 500]
        ).delete()

    for start in range(0, len(fingerprints), 500):
        Account.objects.bulk_update(
            fingerprints[start:start + 500], ['fingerprint']
        )


class Migration(migrations.Migration):

    dependencies = [
        ('vault', '0001_initial'),
    ]

    operations = [
        # RFC 5321 caps an email address at 254 characters.
        migrations.AlterField(
            model_name='email',
            name='name',
            field=models.CharField(
                max_length=254, unique=True,
                verbose_name='Name of the email',
            ),
        ),
        migrations.AddField(
            model_name='account',
            name='fingerprint',
            field=models.CharField(
                max_length=64, null=True, editable=False, blank=True,
                verbose_name='Record fingerprint',
            ),
        ),
        migrations.RunPython(
            canonicalize_reference_values,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            deduplicate_and_fingerprint_accounts,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='account',
            name='fingerprint',
            field=models.CharField(
                editable=False, max_length=64, unique=True,
                verbose_name='Record fingerprint',
            ),
        ),
    ]
