"""
Custom query lookups for the reference tables.

``name__contains_ci`` is a case-insensitive substring match, like Django's
``icontains``, with one difference that matters at scale: on PostgreSQL it is
compiled to ``name ILIKE '%value%'`` instead of ``UPPER(name) LIKE ...``.

The ``UPPER(...)`` form that Django uses for ``icontains`` can never use an
index, so every substring search would read the whole reference table. A plain
``ILIKE`` can use the trigram (``pg_trgm``) GIN index created by migration
``0004``, which turns a substring search over hundreds of millions of values
into an index lookup. On SQLite the lookup behaves exactly like ``icontains``.
"""

from django.db.models import CharField, TextField
from django.db.models.lookups import IContains


class ContainsIgnoreCase(IContains):
    lookup_name = 'contains_ci'

    def get_rhs_op(self, connection, rhs):
        # Non-PostgreSQL backends use their icontains operator unchanged.
        return connection.operators['icontains'] % rhs

    def as_postgresql(self, compiler, connection):
        lhs_sql, params = self.process_lhs(compiler, connection)
        rhs_sql, rhs_params = self.process_rhs(compiler, connection)
        return f'{lhs_sql} ILIKE {rhs_sql}', [*params, *rhs_params]


CharField.register_lookup(ContainsIgnoreCase)
TextField.register_lookup(ContainsIgnoreCase)
