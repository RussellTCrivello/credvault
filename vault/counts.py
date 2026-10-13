"""Exact counts and display formatting for the vault's paginated views.

The UI intentionally reports the real total and lets Django's paginator reach
all pages; it does not stop at a configured row threshold. PostgreSQL must
scan matching index/table entries for an exact ``COUNT(*)``, so broad counts
can take time on very large datasets. Results themselves remain paginated and
exports stream every match.
"""


def exact_count(queryset):
    """Return the exact number of rows represented by a queryset."""
    return queryset.order_by().count()


def count_label(number):
    """Format an exact count with thousands separators."""
    return f'{number:,}'
