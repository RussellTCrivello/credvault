"""
Bounded counting and pagination for very large tables.

``COUNT(*)`` has to read every matching row, so on a table with billions of
records a page load that asks for the exact total can take minutes. The pages
in this workspace only need to show "how many" roughly, so counts stop at a
cap (``VAULT_COUNT_CAP``, default 10,000) and are shown as "10,000+". The cost
of a count is then bounded by the cap, however large the table grows.

Full results are never truncated for export: CSV and JSON exports stream every
matching row.
"""

from functools import cached_property

from django.conf import settings
from django.core.paginator import Paginator

DEFAULT_COUNT_CAP = 10000


def count_cap():
    """The current cap (read at call time so settings overrides apply)."""
    return getattr(settings, 'VAULT_COUNT_CAP', DEFAULT_COUNT_CAP)


def capped_count(queryset, cap=None):
    """
    Return ``(number, capped)``.

    ``number`` is the exact count when it is at most ``cap``; otherwise it is
    ``cap`` and ``capped`` is True. The database reads at most ``cap + 1``
    rows to decide.
    """
    cap = count_cap() if cap is None else cap
    limited = queryset.order_by().values('pk')[: cap + 1]
    found = limited.count()
    if found > cap:
        return cap, True
    return found, False


def count_label(number, capped):
    """Display form of a capped count, e.g. ``10,000+``."""
    text = f'{number:,}'
    return f'{text}+' if capped else text


class CappedPaginator(Paginator):
    """
    Paginator whose total is capped, so page links never trigger a full
    ``COUNT(*)``. With the default cap the last reachable page is
    ``cap / per_page``; the page template says the total is "10,000+".
    """

    def __init__(self, object_list, per_page, cap=None, **kwargs):
        self.cap = cap
        self.capped = False
        super().__init__(object_list, per_page, **kwargs)

    @cached_property
    def count(self):
        number, capped = capped_count(self.object_list, self.cap)
        self.capped = capped
        return number
