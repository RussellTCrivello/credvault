"""
Building the record queryset for searches, filters and exports.

Every search has the same two steps:

1. Turn each typed value into the ids of the reference rows it matches
   (URL, email, username, password, IP). Those lookups use the indexes on the
   reference tables.
2. Select the records that point at those ids. Each record is reached through
   its ``(foreign key, -id)`` index, so results come out newest first without a
   sort, and a page of 100 is read in the time it takes to read 100 index
   entries.

On PostgreSQL the matched ids are sent as a literal list (``url_id IN (1, 7)``).
PostgreSQL then combines the per-column indexes with a bitmap OR. Subqueries
combined with OR (``url_id IN (SELECT ...) OR password_id IN (SELECT ...)``)
often make it read the whole records table instead. Lists longer than the
profile's ``literal_id_limit`` fall back to subqueries, which stay correct and
are simply slower for very broad matches.
"""

from datetime import date
from urllib.parse import urlparse

from django.db.models import Q
from django.db.models.functions import Lower

from .db import is_postgresql
from .models import Account, Email, Ip, Pass, Url, User
from .performance import query_profile
from .utils import extract_domain

#: Quick-search choice -> (reference model, record foreign-key column).
QUICK_SEARCH_MAP = {
    'url': (Url, 'url_id'),
    'email': (Email, 'email_id'),
    'user': (User, 'user_id'),
    'password': (Pass, 'password_id'),
    'ip': (Ip, 'ip_id'),
}

#: Every reference kind with the record column that points at it.
REFERENCE_COLUMNS = (
    (Url, 'url_id'),
    (Email, 'email_id'),
    (User, 'user_id'),
    (Pass, 'password_id'),
    (Ip, 'ip_id'),
)

_RECORD_SELECT = ('url', 'email', 'user', 'password', 'ip')


def has_search_query(params):
    """Return True if request query parameters contain an active search filter."""
    if not params:
        return False
    if (params.get('q') or '').strip() or (params.get('keyword') or '').strip():
        return True
    if params.get('adv') == '1':
        return True
    for key in (
        'url', 'account_type', 'email', 'username',
        'password', 'ip', 'created_from', 'created_to',
    ):
        if (params.get(key) or '').strip():
            return True
    return False


def _ids_for(queryset, profile):
    """
    Return ``(ids, found)`` for a reference lookup.

    ``ids`` is a literal list on PostgreSQL when the match is small enough,
    otherwise a subquery. ``found`` tells whether any reference row matched.
    """
    if is_postgresql(queryset.db):
        limit = profile.literal_id_limit
        ids = list(queryset.values_list('id', flat=True)[:limit + 1])
        if len(ids) <= limit:
            return ids, bool(ids)
        return queryset.values('id'), True
    return queryset.values('id'), queryset.exists()


def prefix_clause(prefix):
    """
    Case-insensitive 'starts with' on a queryset annotated ``_n=Lower('name')``.

    Use it with ``annotate(_n=Lower('name'))``.
    """
    if is_postgresql():
        # LIKE uses the text_pattern_ops index under every collation.
        return Q(_n__startswith=prefix)
    # SQLite: a range on the lower-cased name uses its B-tree index.
    return Q(_n__gte=prefix, _n__lt=prefix + '\uffff')


def _exact_ids(model, raw_value, profile):
    """Ids of reference rows whose name equals ``raw_value`` ignoring case."""
    val = (raw_value or '').strip().lower()
    if not val:
        return [], False
    return _ids_for(
        model.objects.annotate(_n=Lower('name')).filter(_n=val), profile
    )


def _is_domain_like_query(q_lower):
    """
    Return True when the query represents a domain or bare origin (such as
    'example.com' or 'https://example.com') rather than a full URL with a
    specific path or a plain word without a TLD dot.
    """
    if '://' in q_lower:
        try:
            parsed = urlparse(q_lower)
            return bool(parsed.netloc) and parsed.path in ('', '/')
        except Exception:
            return False
    return '.' in q_lower and '/' not in q_lower and '@' not in q_lower


def _url_ids(raw_value, profile, force_domain=False):
    """
    Ids of the URLs matching ``raw_value``.

    - Always matches the exact URL (case-insensitive).
    - When the query is a domain (e.g. 'example.com') or force_domain=True,
      also matches URLs hosted on that domain (https://domain/...,
      http://domain/..., https://www.domain/..., and so on).
    """
    q_lower = (raw_value or '').strip().lower()
    if not q_lower:
        return [], False

    exact_set = {q_lower, q_lower.rstrip('/')}
    clauses = None

    if force_domain or _is_domain_like_query(q_lower):
        dom = extract_domain(q_lower)
        if dom and (force_domain or '.' in dom):
            hosts = [dom]
            if not dom.startswith('www.'):
                hosts.append('www.' + dom)
            for scheme in ('https://', 'http://', ''):
                for host in hosts:
                    base = f"{scheme}{host}"
                    exact_set.add(base)
                    for sep in ('/', ':', '?', '#'):
                        clause = prefix_clause(base + sep)
                        clauses = clause if clauses is None else clauses | clause

    match = Q(_n__in=exact_set)
    if clauses is not None:
        match = match | clauses

    ids, found = _ids_for(
        Url.objects.annotate(_n=Lower('name')).filter(match), profile
    )
    if not found and force_domain:
        dom = extract_domain(q_lower)
        if dom:
            ids, found = _ids_for(
                Url.objects.filter(name__contains_ci=dom), profile
            )
    return ids, found


def _keyword_clauses(keyword, profile):
    """Records whose URL, email, username, password or IP contains ``keyword``."""
    clauses = Q()
    for model, fk in REFERENCE_COLUMNS:
        ids, _found = _ids_for(
            model.objects.filter(name__contains_ci=keyword), profile
        )
        clauses |= Q(**{f'{fk}__in': ids})
    return clauses


def _advanced_queryset(params, qs, keyword, profile):
    """The advanced-search filters: each field is optional and they are AND-ed."""
    if keyword:
        qs = qs.filter(_keyword_clauses(keyword, profile))

    url = (params.get('url') or '').strip()
    if url:
        ids, _found = _ids_for(
            Url.objects.filter(name__contains_ci=url), profile
        )
        qs = qs.filter(url_id__in=ids)

    account_type = (params.get('account_type') or '').strip()
    if account_type:
        qs = qs.filter(account_type=account_type)

    for param, model, fk in (
        ('email', Email, 'email_id'),
        ('username', User, 'user_id'),
        ('password', Pass, 'password_id'),
        ('ip', Ip, 'ip_id'),
    ):
        value = (params.get(param) or '').strip()
        if value:
            ids, _found = _ids_for(
                model.objects.filter(name__contains_ci=value), profile
            )
            qs = qs.filter(**{f'{fk}__in': ids})

    for param, lookup in (('created_from', 'gte'), ('created_to', 'lte')):
        raw = (params.get(param) or '').strip()
        if not raw:
            continue
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            # An unreadable date matches nothing; the form reports the error.
            return qs.none()
        qs = qs.filter(**{f'created_at__date__{lookup}': day})

    sort = params.get('sort') or 'newest'
    if sort == 'oldest':
        qs = qs.order_by('id')
    elif sort == 'url':
        qs = qs.order_by('url__name', 'id')
    elif sort == 'identifier':
        qs = qs.order_by('email__name', 'user__name', 'id')
    else:
        qs = qs.order_by('-id')
    return qs


def _keyword_fallback(qs, keyword, profile):
    return qs.filter(_keyword_clauses(keyword, profile))


def filter_accounts(params, profile=None):
    """
    Build an Account queryset from URL query parameters.

    When no query is supplied, returns an empty queryset so records are never
    dumped without a query. ``profile`` defaults to the saved query mode.
    """
    profile = profile or query_profile()
    qs = Account.objects.select_related(*_RECORD_SELECT)

    field = (params.get('field') or '').strip()
    q = (params.get('q') or '').strip()
    keyword = (params.get('keyword') or '').strip()

    if params.get('adv') == '1':
        return _advanced_queryset(params, qs, keyword, profile)

    search_term = q or keyword
    if not search_term:
        return qs.none()

    if field == 'domain':
        ids, _found = _url_ids(search_term, profile, force_domain=True)
        return qs.filter(url_id__in=ids)

    if field == 'url':
        ids, _found = _url_ids(search_term, profile, force_domain=False)
        return qs.filter(url_id__in=ids)

    if field in QUICK_SEARCH_MAP:
        model, fk = QUICK_SEARCH_MAP[field]
        ids, _found = _exact_ids(model, search_term, profile)
        return qs.filter(**{f'{fk}__in': ids})

    # 'all', an unspecified field, or 'keyword': check the five reference
    # tables (URL, email, username, password, IP) in turn.
    lookups = (
        ('url_id', _url_ids(search_term, profile, force_domain=False)),
        ('email_id', _exact_ids(Email, search_term, profile)),
        ('user_id', _exact_ids(User, search_term, profile)),
        ('password_id', _exact_ids(Pass, search_term, profile)),
        ('ip_id', _exact_ids(Ip, search_term, profile)),
    )
    clauses = Q()
    found_any = False
    for fk, (ids, found) in lookups:
        if found:
            clauses |= Q(**{f'{fk}__in': ids})
            found_any = True
    if found_any:
        return qs.filter(clauses)

    # Free-text keyword with no exact or domain match: substring lookup.
    if keyword and not q:
        return _keyword_fallback(qs, keyword, profile)

    return qs.none()
