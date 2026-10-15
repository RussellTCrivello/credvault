"""Exact counts and display formatting for the vault's paginated views.

The UI intentionally reports the real total and lets Django's paginator reach
all pages; it does not stop at a configured row threshold. PostgreSQL must
scan matching index/table entries for an exact ``COUNT(*)``, so broad counts
can take time on very large datasets. Results themselves remain paginated and
exports stream every match.
"""

from django.core.cache import cache


#: Dashboard and browse-page counts are expensive on very large tables.
#: Cache them briefly so repeated page loads within a few seconds reuse the
#: result instead of re-scanning.  Import completion invalidates explicitly.
_COUNT_CACHE_TTL = 15  # seconds


def exact_count(queryset):
    """Return the exact number of rows represented by a queryset."""
    return queryset.order_by().count()


def count_label(number):
    """Format an exact count with thousands separators."""
    return f'{number:,}'


def cached_model_count(model, key=None):
    """Cached ``COUNT(*)`` for an entire model table.

    The cache key defaults to ``model._meta.db_table`` so it is stable
    across requests.  Callers that need finer granularity can pass a
    custom key.  The TTL is short; import completion invalidates via
    ``invalidate_count_cache``.
    """
    cache_key = f'vault_count:{key or model._meta.db_table}'
    value = cache.get(cache_key)
    if value is not None:
        return value
    count = model.objects.order_by().count()
    cache.set(cache_key, count, _COUNT_CACHE_TTL)
    return count


def invalidate_count_cache(*models):
    """Drop cached counts for the given models (called after import)."""
    for model in models:
        cache.delete(f'vault_count:{model._meta.db_table}')
