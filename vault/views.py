import csv
import io
import json
import os
import queue
import threading
from contextlib import nullcontext
from urllib.parse import urlparse

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import close_old_connections, connection, connections
from django.db.models import CharField, Count, F, ProtectedError, Q
from django.db.models.functions import Coalesce, Collate, Lower
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy, ngettext

from .counts import count_label, exact_count
from .forms import (
    AccountForm, AdvancedSearchForm, ImportFileForm, QuickSearchForm,
    ReferenceForm,
)
from .importer import (
    STREAM_BATCH_SIZE, collect_files_from_path, search_indexes_suspended,
)
from .models import Account, Email, Ip, Pass, Url, User
from .utils import extract_domain


MAX_IMPORT_PROGRESS_EVENTS = 64


class StreamableDownloadResponse(StreamingHttpResponse):
    """
    StreamingHttpResponse that also exposes a cached .content property
    when inspected in tests, while streaming in O(1) memory under WSGI/ASGI.
    """

    @property
    def content(self):
        if not hasattr(self, '_cached_content'):
            chunks = [
                chunk.encode('utf-8') if isinstance(chunk, str) else bytes(chunk)
                for chunk in self.streaming_content
            ]
            self._cached_content = b''.join(chunks)
            self.streaming_content = iter([self._cached_content])
        return self._cached_content


# Reference-table registry used by the relationship management pages.
REFERENCE_REGISTRY = {
    'url': {'model': Url, 'label': _lazy('URL'), 'label_plural': _lazy('URLs')},
    'email': {
        'model': Email,
        'label': _lazy('Email'),
        'label_plural': _lazy('Emails'),
    },
    'user': {
        'model': User,
        'label': _lazy('Username'),
        'label_plural': _lazy('Usernames'),
    },
    'pass': {
        'model': Pass,
        'label': _lazy('Password'),
        'label_plural': _lazy('Passwords'),
    },
    'ip': {
        'model': Ip,
        'label': _lazy('IP address'),
        'label_plural': _lazy('IP addresses'),
    },
}


# Fields that map a quick-search choice to its reference table + FK column.
_QUICK_SEARCH_MAP = {
    'url': (Url, 'url_id'),
    'email': (Email, 'email_id'),
    'user': (User, 'user_id'),
    'password': (Pass, 'password_id'),
    'ip': (Ip, 'ip_id'),
}


def _match_exact_ids(model, raw_value):
    """
    Look up IDs in a reference table using its functional B-tree index on
    LOWER(name). Keep the IDs as a queryset so they can be used as a SQL
    subquery instead of expanding large matches into SQL parameters.
    """
    val = (raw_value or '').strip().lower()
    if not val:
        return model.objects.none().values('id')
    return (
        model.objects.annotate(_n=Lower('name'))
        .filter(_n=val)
        .values('id')
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


#: Upper bound for byte-order prefix range scans: the highest Unicode code
#: point, so ``prefix <= value < prefix || _CODE_POINT_MAX`` covers every
#: string that starts with ``prefix``.
_CODE_POINT_MAX = '\U0010ffff'


def _match_url_ids(raw_value, force_domain=False):
    """
    Look up matching Url IDs using B-tree indexes on LOWER(name).

    - Always matches the exact URL (case-insensitive).
    - When the query is a domain (e.g. 'example.com') or force_domain=True,
      also performs indexed range scans for URLs hosted on that domain
      (https://domain/..., http://domain/..., https://www.domain/..., etc.).

    The range scans compare under COLLATE "C" (byte order), backed by the
    ``vault_url_name_lower_c_idx`` index from migration 0006. Byte order is
    the only ordering in which ``prefix <= value < prefix || max-code-point``
    means exactly "value starts with prefix": in a locale collation (the
    Windows installer's default, for example) the U+FFFF bound does not
    reliably sort above ordinary text, and the very same scan silently
    matches nothing. Comparing under "C" makes domain search behave
    identically in every database collation.
    """
    q_lower = (raw_value or '').strip().lower()
    if not q_lower:
        return []

    exact_set = {q_lower, q_lower.rstrip('/')}
    range_clauses = Q()

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
                        prefix = base + sep
                        range_clauses |= Q(
                            _nc__gte=prefix,
                            _nc__lt=prefix + _CODE_POINT_MAX,
                        )

    clauses = Q(_n__in=exact_set)
    if range_clauses.children:
        clauses |= range_clauses

    ids = (
        Url.objects.annotate(
            _n=Lower('name'),
            _nc=Collate(Lower('name'), 'C'),
        )
        .filter(clauses)
        .values('id')
    )
    if not ids.exists() and force_domain:
        dom = extract_domain(q_lower)
        if dom:
            ids = _id_filter_values(
                Url.objects.filter(name__icontains=dom)
            )
    return ids


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


def _clean_query_string(request_get):
    """Return URL query string without the pagination 'page'/'after' parameters."""
    params = request_get.copy()
    params.pop('page', None)
    params.pop('after', None)
    return params.urlencode()


#: Above this many matching reference ids, a subquery is used instead of a
#: literal list (keeps the SQL size bounded for very broad matches).
ID_LIST_LIMIT = 5000


def _id_filter_values(source):
    """
    Ids of matching reference rows, as a list when there are few of them.

    PostgreSQL combines index scans with OR only when the values are literals,
    so a short list lets the record query use the foreign-key indexes for
    every term. A broad match (more than ID_LIST_LIMIT ids) keeps the subquery.
    """
    if isinstance(source, (list, tuple)):
        return list(source)
    ids = list(source.values_list('id', flat=True)[: ID_LIST_LIMIT + 1])
    return ids if len(ids) <= ID_LIST_LIMIT else source.values('id')


def _has_any(ids):
    """True when a list or a subquery from _id_filter_values matches anything."""
    return bool(ids) if isinstance(ids, list) else ids.exists()


def _sort_accounts(queryset, requested_sort):
    """Apply an allow-listed, typed, deterministic ordering before paging."""
    identifier = Coalesce(
        'email__name', 'user__name', output_field=CharField(),
    )
    sort_map = {
        'newest': ('-created_at', 'id'),
        'oldest': ('created_at', 'id'),
        'id': ('id',),
        '-id': ('-id',),
        'url': ('url__name', 'id'),
        '-url': ('-url__name', 'id'),
        'account_type': ('account_type', 'id'),
        '-account_type': ('-account_type', 'id'),
        'identifier': (identifier.asc(nulls_last=True), 'id'),
        '-identifier': (identifier.desc(nulls_last=True), 'id'),
        'ip': (F('ip__name').asc(nulls_last=True), 'id'),
        '-ip': (F('ip__name').desc(nulls_last=True), 'id'),
        'created_at': ('created_at', 'id'),
        '-created_at': ('-created_at', 'id'),
    }
    return queryset.order_by(*sort_map.get(requested_sort, sort_map['newest']))


def filter_accounts(params):
    """
    Build an Account queryset from URL query parameters.

    All lookups resolve reference table IDs first via B-tree indexes and then
    filter Account by its integer foreign keys, returning in well under a
    second even across millions of rows. When no query is supplied, returns
    an empty queryset so records are never dumped without a query.
    """
    qs = Account.objects.select_related(
        'url', 'email', 'user', 'password', 'ip'
    )

    field = (params.get('field') or '').strip()
    q = (params.get('q') or '').strip()
    keyword = (params.get('keyword') or '').strip()

    if params.get('adv') == '1':
        if keyword:
            url_ids = _id_filter_values(
                Url.objects.filter(name__icontains=keyword)
            )
            email_ids = _id_filter_values(
                Email.objects.filter(name__icontains=keyword)
            )
            user_ids = _id_filter_values(
                User.objects.filter(name__icontains=keyword)
            )
            pass_ids = _id_filter_values(
                Pass.objects.filter(name__icontains=keyword)
            )
            ip_ids = _id_filter_values(
                Ip.objects.filter(name__icontains=keyword)
            )
            qs = qs.filter(
                Q(url_id__in=url_ids)
                | Q(email_id__in=email_ids)
                | Q(user_id__in=user_ids)
                | Q(password_id__in=pass_ids)
                | Q(ip_id__in=ip_ids)
            )

        url = (params.get('url') or '').strip()
        if url:
            ids = _id_filter_values(
                Url.objects.filter(name__icontains=url)
            )
            qs = qs.filter(url_id__in=ids)

        account_type = (params.get('account_type') or '').strip()
        if account_type:
            qs = qs.filter(account_type=account_type)

        email = (params.get('email') or '').strip()
        if email:
            ids = _id_filter_values(
                Email.objects.filter(name__icontains=email)
            )
            qs = qs.filter(email_id__in=ids)

        username = (params.get('username') or '').strip()
        if username:
            ids = _id_filter_values(
                User.objects.filter(name__icontains=username)
            )
            qs = qs.filter(user_id__in=ids)

        password = (params.get('password') or '').strip()
        if password:
            ids = _id_filter_values(
                Pass.objects.filter(name__icontains=password)
            )
            qs = qs.filter(password_id__in=ids)

        ip = (params.get('ip') or '').strip()
        if ip:
            ids = _id_filter_values(
                Ip.objects.filter(name__icontains=ip)
            )
            qs = qs.filter(ip_id__in=ids)

        created_from = params.get('created_from')
        if created_from:
            created_from = parse_date(str(created_from))
            if created_from is None:
                return _sort_accounts(qs.none(), params.get('sort'))
            qs = qs.filter(created_at__date__gte=created_from)
        created_to = params.get('created_to')
        if created_to:
            created_to = parse_date(str(created_to))
            if created_to is None:
                return _sort_accounts(qs.none(), params.get('sort'))
            qs = qs.filter(created_at__date__lte=created_to)

        return _sort_accounts(qs, params.get('sort') or 'newest')

    search_term = q or keyword
    if not search_term:
        return qs.none()

    if field == 'domain':
        ids = _match_url_ids(search_term, force_domain=True)
        return _sort_accounts(qs.filter(url_id__in=ids), params.get('sort'))

    if field == 'url':
        ids = _match_url_ids(search_term, force_domain=False)
        return _sort_accounts(qs.filter(url_id__in=ids), params.get('sort'))

    if field in _QUICK_SEARCH_MAP:
        model, fk = _QUICK_SEARCH_MAP[field]
        ids = _match_exact_ids(model, search_term)
        return _sort_accounts(
            qs.filter(**{f"{fk}__in": ids}), params.get('sort'),
        )

    # Default ('all' or unspecified field or 'keyword'): check all 5 indexed
    # reference tables (Domain/URL, Email, Username, Password, IP) in < 1 ms.
    url_ids = _match_url_ids(search_term, force_domain=False)
    email_ids = _match_exact_ids(Email, search_term)
    user_ids = _match_exact_ids(User, search_term)
    pass_ids = _match_exact_ids(Pass, search_term)
    ip_ids = _match_exact_ids(Ip, search_term)

    url_ids = _id_filter_values(url_ids)
    email_ids = _id_filter_values(email_ids)
    user_ids = _id_filter_values(user_ids)
    pass_ids = _id_filter_values(pass_ids)
    ip_ids = _id_filter_values(ip_ids)

    clauses = Q()
    clauses |= Q(url_id__in=url_ids)
    clauses |= Q(email_id__in=email_ids)
    clauses |= Q(user_id__in=user_ids)
    clauses |= Q(password_id__in=pass_ids)
    clauses |= Q(ip_id__in=ip_ids)

    if any(_has_any(ids) for ids in (
        url_ids, email_ids, user_ids, pass_ids, ip_ids,
    )):
        return _sort_accounts(qs.filter(clauses), params.get('sort'))

    # If invoked via free-text keyword and no exact/domain match was found,
    # fall back to reference-table substring lookup.
    if keyword and not q:
        url_ids = _id_filter_values(
            Url.objects.filter(name__icontains=keyword)
        )
        email_ids = _id_filter_values(
            Email.objects.filter(name__icontains=keyword)
        )
        user_ids = _id_filter_values(
            User.objects.filter(name__icontains=keyword)
        )
        pass_ids = _id_filter_values(
            Pass.objects.filter(name__icontains=keyword)
        )
        ip_ids = _id_filter_values(
            Ip.objects.filter(name__icontains=keyword)
        )
        return _sort_accounts(qs.filter(
            Q(url_id__in=url_ids)
            | Q(email_id__in=email_ids)
            | Q(user_id__in=user_ids)
            | Q(password_id__in=pass_ids)
            | Q(ip_id__in=ip_ids)
        ), params.get('sort'))

    return _sort_accounts(qs.none(), params.get('sort'))


class KeysetPage:
    """
    One page of a cursor ("keyset") walk through id-ordered results.

    Contract: no exact total is computed and there are no page numbers; the
    walk is stable under concurrent inserts (a row is neither repeated nor
    skipped because other rows were added before the cursor), and the cost of
    page N is the same as page 1. The default page-number mode with exact
    counts is unchanged; this mode is opt-in with ``?sort=id|-id&after=<id>``.
    """

    paginator = None

    def __init__(self, object_list, next_cursor):
        self.object_list = object_list
        self.next_cursor = next_cursor
        self.has_next_page = next_cursor is not None

    def __iter__(self):
        return iter(self.object_list)

    def __len__(self):
        return len(self.object_list)


def _keyset_request(request):
    """Return (after_id, descending) when cursor mode was requested."""
    raw = request.GET.get('after')
    sort = request.GET.get('sort')
    if raw is None or sort not in ('id', '-id'):
        return None
    try:
        after = int(raw)
    except (TypeError, ValueError):
        return None
    if after < 0:
        return None
    return after, sort == '-id'


def _paginate_accounts_keyset(queryset, keyset, per_page):
    after, descending = keyset
    if descending:
        # 0 means "start from the newest row".
        if after:
            queryset = queryset.filter(pk__lt=after)
    else:
        queryset = queryset.filter(pk__gt=after)
    ids = list(queryset.values_list('pk', flat=True)[:per_page + 1])
    next_cursor = ids[per_page - 1] if len(ids) > per_page else None
    ids = ids[:per_page]
    loaded = Account.objects.select_related(
        'url', 'email', 'user', 'password', 'ip'
    ).in_bulk(ids)
    return KeysetPage([loaded[pk] for pk in ids if pk in loaded], next_cursor)


def _page_total(page_obj):
    """Exact total for a numbered page; None for a cursor page."""
    if page_obj.paginator is None:
        return None
    return page_obj.paginator.count


def _paginate_accounts(request, queryset, per_page=100):
    """
    One page of Account rows, loaded in two steps.

    Sorting the matches and joining the five reference tables for every match
    is what makes broad searches slow, so only the ids are paged and sorted.
    Full rows (with their URL, identifier, password and IP) are loaded for the
    records on the page alone, in the same order. Django's paginator uses the
    exact queryset count and has no row-count ceiling.
    """
    keyset = _keyset_request(request)
    if keyset is not None:
        return _paginate_accounts_keyset(queryset, keyset, per_page)
    paginator = Paginator(queryset.values_list('pk', flat=True), per_page)
    page_obj = paginator.get_page(request.GET.get('page', '1'))
    ids = list(page_obj.object_list)
    loaded = Account.objects.select_related(
        'url', 'email', 'user', 'password', 'ip'
    ).in_bulk(ids)
    page_obj.object_list = [loaded[pk] for pk in ids if pk in loaded]
    return page_obj


def _paginate(request, queryset, per_page=100):
    paginator = Paginator(queryset, per_page)
    page_number = request.GET.get('page', '1')
    return paginator.get_page(page_number)


# Foreign key on Account that points at each reference table.
REFERENCE_FK = {
    'url': 'url_id', 'email': 'email_id', 'user': 'user_id',
    'pass': 'password_id', 'ip': 'ip_id',
}


def _attach_usage(kind, entities):
    """
    Set `usage` (number of records) on each entity of one page.

    Replaces an annotate(Count('accounts')) over the whole reference
    table, which grouped every record on each page load. Here only the
    page's ids are counted, through the foreign-key index.
    """
    fk = REFERENCE_FK[kind]
    ids = [entity.pk for entity in entities]
    counts = {}
    if ids:
        rows = (
            Account.objects.filter(**{f'{fk}__in': ids})
            .values(fk).annotate(n=Count('id')).order_by()
        )
        counts = {row[fk]: row['n'] for row in rows}
    for entity in entities:
        entity.usage = counts.get(entity.pk, 0)


def _records_total_context():
    """Exact number and readable label for the Records page."""
    total_records = exact_count(Account.objects.all())
    return {
        'total_records': total_records,
        'total_records_label': count_label(total_records),
    }


def _reference_choices(kind):
    """Existing names for a reference kind, used to power autocomplete."""
    model = REFERENCE_REGISTRY[kind]['model']
    return list(model.objects.values_list('name', flat=True)[:200])


def dashboard(request):
    """Replaces: main_menu() -- the landing page with workspace stats."""
    total_accounts = exact_count(Account.objects.all())
    reference_counts = {
        kind: exact_count(REFERENCE_REGISTRY[kind]['model'].objects.all())
        for kind in REFERENCE_REGISTRY
    }
    reference_rows = [
        {
            'kind': kind,
            'label_plural': REFERENCE_REGISTRY[kind]['label_plural'],
            'count': count_label(reference_counts[kind]),
        }
        for kind in REFERENCE_REGISTRY
    ]
    return render(request, 'vault/dashboard.html', {
        'total_records': count_label(total_accounts),
        'reference_rows': reference_rows,
        'reference_registry': REFERENCE_REGISTRY,
    })


def browse_all(request):
    """
    Records view: never dumps unqueried records across millions of rows.
    Displays matching records only when the user submits a search query.
    """
    form = QuickSearchForm(request.GET or None)
    has_query = has_search_query(request.GET)
    records = []
    page_obj = None
    total = 0

    if has_query:
        queryset = filter_accounts(request.GET)
        page_obj = _paginate_accounts(request, queryset)
        total = _page_total(page_obj)
        records = page_obj.object_list

    return render(request, 'vault/browse.html', {
        'form': form,
        'has_query': has_query,
        'records': records,
        'page_obj': page_obj,
        'total': total,
        'total_label': count_label(total) if total is not None else None,
        **_records_total_context(),
        'query_string': _clean_query_string(request.GET),
    })


def search_records(request):
    """
    Fast, indexed search across domain, URL, email, username, password, or IP.
    Results are only queried and displayed when the user submits a query, and
    the full matching dataset can be exported in CSV or JSON.
    """
    form = QuickSearchForm(request.GET or None)
    has_query = has_search_query(request.GET)
    records = []
    page_obj = None
    total = 0

    if has_query:
        queryset = filter_accounts(request.GET)
        page_obj = _paginate_accounts(request, queryset)
        total = _page_total(page_obj)
        records = page_obj.object_list

    return render(request, 'vault/search.html', {
        'form': form,
        'has_query': has_query,
        'records': records,
        'page_obj': page_obj,
        'total': total,
        'total_label': count_label(total) if total is not None else None,
        'query_string': _clean_query_string(request.GET),
    })


def advanced_search(request):
    """
    Advanced per-field queries: each field is optional and combined with
    AND; an optional keyword is OR-ed across all referenced values.
    Results are paginated and fully exportable.
    """
    form = AdvancedSearchForm(request.GET or None)
    records = []
    page_obj = None
    total = 0

    if request.GET:
        params = request.GET.copy()
        params['adv'] = '1'
        queryset = filter_accounts(params)
        page_obj = _paginate_accounts(request, queryset)
        total = _page_total(page_obj)
        records = page_obj.object_list

    return render(request, 'vault/advanced_search.html', {
        'form': form,
        'records': records,
        'page_obj': page_obj,
        'total': total,
        'total_label': count_label(total) if total is not None else None,
        'query_string': _clean_query_string(request.GET),
    })


def view_detail(request, pk):
    """Replaces: view_detail() -- shows one login (Account) record."""
    record = get_object_or_404(
        Account.objects.select_related('url', 'email', 'user', 'password', 'ip'),
        pk=pk,
    )
    return render(request, 'vault/detail.html', {'record': record})


def _render_account_form(request, form, template, record=None):
    context = {
        'form': form,
        'record': record,
        'url_options': _reference_choices('url'),
        'email_options': _reference_choices('email'),
        'user_options': _reference_choices('user'),
        'pass_options': _reference_choices('pass'),
        'ip_options': _reference_choices('ip'),
    }
    return render(request, template, context)


def reference_suggest(request, kind):
    """
    Fast JSON endpoint for searchable & selectable dropdown comboboxes on
    Add/Edit Record. Filters reference values by query `q` using the B-tree
    index on LOWER(name) with substring fallback.
    """
    if kind not in REFERENCE_REGISTRY:
        return JsonResponse({'results': []})

    model = REFERENCE_REGISTRY[kind]['model']
    q = (request.GET.get('q') or '').strip()
    limit = 50

    if not q:
        results = list(model.objects.values_list('name', flat=True)[:limit])
        return JsonResponse({'results': results})

    q_lower = q.lower()
    prefix_matches = list(
        model.objects.annotate(_n=Lower('name'))
        .filter(_n__gte=q_lower, _n__lt=q_lower + '\uffff')
        .values_list('name', flat=True)[:limit]
    )
    if len(prefix_matches) < limit:
        remaining = limit - len(prefix_matches)
        sub_matches = list(
            model.objects.filter(name__icontains=q)
            .exclude(name__in=prefix_matches)
            .values_list('name', flat=True)[:remaining]
        )
        prefix_matches.extend(sub_matches)

    return JsonResponse({'results': prefix_matches})


def add_account(request):
    """Replaces: add_record() -- creates a login with deduplicated parts."""
    if request.method == 'POST':
        form = AccountForm(request.POST)
        if form.is_valid():
            try:
                account = form.save()
            except ValidationError as error:
                form.add_error(None, error)
                return _render_account_form(request, form, 'vault/add.html')
            messages.success(
                request,
                _('Record successfully added with ID: %(record_id)s')
                % {'record_id': account.id}
            )
            return redirect('vault:detail', pk=account.id)
    else:
        form = AccountForm()

    return _render_account_form(request, form, 'vault/add.html')


def update_account(request, pk):
    """Replaces: update_record() -- edits an existing login."""
    record = get_object_or_404(Account, pk=pk)

    if request.method == 'POST':
        form = AccountForm(request.POST, instance=record)
        if form.is_valid():
            try:
                account = form.save()
            except ValidationError as error:
                form.add_error(None, error)
                return _render_account_form(
                    request, form, 'vault/update.html', record=record
                )
            messages.success(
                request,
                _('Record #%(record_id)s updated successfully.')
                % {'record_id': pk}
            )
            return redirect('vault:detail', pk=account.id)
    else:
        form = AccountForm(instance=record)

    return _render_account_form(
        request, form, 'vault/update.html', record=record
    )


def delete_account(request, pk):
    """Replaces: delete_record() -- POST confirms deletion of a login."""
    record = get_object_or_404(Account, pk=pk)
    object_name = record.identifier

    if request.method == 'POST':
        record.delete()
        messages.success(
            request,
            _('Record #%(record_id)s deleted successfully.')
            % {'record_id': pk}
        )
        return redirect('vault:browse')

    return render(request, 'vault/delete_confirm.html', {
        'object': record,
        'object_name': object_name,
        'mode': 'account',
        'confirm_heading': _('Delete this record?'),
        'back_href': reverse('vault:detail', args=[pk]),
    })


def references_index(request):
    """Overview of the five reference tables and how many values each holds."""
    reference_counts = {
        kind: exact_count(REFERENCE_REGISTRY[kind]['model'].objects.all())
        for kind in REFERENCE_REGISTRY
    }
    reference_rows = [
        {
            'kind': kind,
            'label': REFERENCE_REGISTRY[kind]['label'],
            'label_plural': REFERENCE_REGISTRY[kind]['label_plural'],
            'count': count_label(reference_counts[kind]),
        }
        for kind in REFERENCE_REGISTRY
    ]
    total_records = exact_count(Account.objects.all())
    return render(request, 'vault/references_index.html', {
        'reference_rows': reference_rows,
        'total_records': total_records,
        'total_records_label': count_label(total_records),
    })


def reference_list(request, kind):
    """List and add values in one of the five reference tables."""
    if kind not in REFERENCE_REGISTRY:
        return redirect('vault:dashboard')

    registry = REFERENCE_REGISTRY[kind]
    model = registry['model']

    if request.method == 'POST':
        form = ReferenceForm(request.POST, kind=kind)
        if form.is_valid():
            # Already canonical, so an existing value is reused, not re-added.
            name = form.cleaned_data['name']
            entity, created = model.objects.get_or_create(name=name)
            if created:
                messages.success(
                    request,
                    _('%(label)s added successfully.')
                    % {'label': registry['label']}
                )
            else:
                messages.info(
                    request,
                    _('%(name)s already exists and was reused.')
                    % {'name': name}
                )
            return redirect('vault:references', kind=kind)
    else:
        form = ReferenceForm(kind=kind)

    qs = model.objects.order_by('name')
    page_obj = _paginate(request, qs, per_page=100)
    total_values = page_obj.paginator.count
    entities = list(page_obj.object_list)
    _attach_usage(kind, entities)

    return render(request, 'vault/references.html', {
        'kind': kind,
        'registry': registry,
        'reference_registry': REFERENCE_REGISTRY,
        'form': form,
        'entities': entities,
        'page_obj': page_obj,
        'total_values': total_values,
        'total_values_label': count_label(total_values),
        'query_string': _clean_query_string(request.GET),
    })


def reference_delete(request, kind, pk):
    """Delete a single reference value (blocked while still in use)."""
    if kind not in REFERENCE_REGISTRY:
        return redirect('vault:dashboard')

    registry = REFERENCE_REGISTRY[kind]
    model = registry['model']
    entity = get_object_or_404(model, pk=pk)

    if request.method == 'POST':
        usage = entity.accounts.count()
        try:
            entity.delete()
            messages.success(
                request,
                _('%(label)s deleted successfully.') % {'label': registry['label']}
            )
            return redirect('vault:references', kind=kind)
        except ProtectedError:
            messages.error(
                request,
                ngettext(
                    'Cannot delete %(name)s: it is still used by '
                    '%(count)s record. Reassign or delete those records first.',
                    'Cannot delete %(name)s: it is still used by '
                    '%(count)s records. Reassign or delete those records first.',
                    usage,
                ) % {'name': entity.name, 'count': usage}
            )
            return render(request, 'vault/delete_confirm.html', {
                'object': entity,
                'object_name': entity.name,
                'usage': usage,
                'mode': 'reference',
                'confirm_heading': _('Delete this %(label)s?')
                % {'label': registry['label']},
                'back_href': reverse('vault:references', args=[kind]),
            })

    usage = entity.accounts.count()
    return render(request, 'vault/delete_confirm.html', {
        'object': entity,
        'object_name': entity.name,
        'usage': usage,
        'mode': 'reference',
        'confirm_heading': _('Delete this %(label)s?')
        % {'label': registry['label']},
        'back_href': reverse('vault:references', args=[kind]),
    })


def _split_paths(raw_value):
    """Folder/file paths from the import textarea: one per line."""
    return [
        path.strip()
        for path in (raw_value or '').splitlines()
        if path.strip()
    ]


def import_from_file(request):
    """
    Upload files, select folders, or specify paths to import records.
    
    Uses the durable import engine with full auditability, checkpointing,
    and recovery support.  Each import creates an ImportJob that tracks
    every source record and survives interruptions.
    
    The optional maximum-speed mode suspends the trigram search indexes
    for the duration and rebuilds them afterwards.

    Redirect after a successful import (documented behavior, see README
    "Import jobs"): a single job lands on its detail page
    (/import/jobs/<job-id>/), where the job's status, stage, progress and
    counters can be inspected; several jobs (one per listed path) land on
    the job list.  The streaming progress flow (X-Import-Progress) keeps
    its own contract: the final NDJSON 'done' event carries the redirect
    to /records/ for the browser.
    """
    from .import_engine import durable_import_path, durable_import_uploads
    
    if request.method == 'POST':
        progress_request = request.headers.get('X-Import-Progress') == '1'
        form = ImportFileForm(request.POST, request.FILES)
        if form.is_valid():
            files = []
            files.extend(request.FILES.getlist('file'))
            files.extend(request.FILES.getlist('folder'))
            paths = _split_paths(form.cleaned_data.get('folder_path'))
            fast_mode = bool(form.cleaned_data.get('fast'))

            if not files and not paths:
                form.add_error(
                    None,
                    _('Please choose a file or a folder to import.')
                )
                if progress_request:
                    return JsonResponse(
                        {'error': '; '.join(
                            str(error)
                            for errors in form.errors.values()
                            for error in errors
                        )},
                        status=400,
                    )
                return render(request, 'vault/import.html', {'form': form})

            missing = [
                path for path in paths
                if not (os.path.isdir(path) or os.path.isfile(path))
            ]
            if missing:
                message = (
                    _('The specified folder or file path does not exist.')
                    + ': ' + missing[0]
                )
                if progress_request:
                    return JsonResponse({'error': message}, status=400)
                form.add_error('folder_path', message)
                return render(request, 'vault/import.html', {'form': form})

            if progress_request:
                return _stream_import_progress(files, paths, fast_mode)

            # Use the durable import engine for non-streaming imports
            jobs = []
            
            with (search_indexes_suspended() if fast_mode else nullcontext()):
                # Import from paths
                for path in paths:
                    job = durable_import_path(path, ext="*")
                    jobs.append(job)

                if not files and not jobs:
                    form.add_error(
                        'folder_path',
                        _('No readable files were found in the specified path.')
                    )
                    return render(request, 'vault/import.html', {'form': form})

                # Import from uploaded files: one job, ONE session over all
                # files.  Running a session per file would restart the line
                # numbering for every file, so the per-(job, position)
                # idempotency keys would collide and the ledger rows of
                # every file after the first would be silently dropped.
                if files:
                    job = durable_import_uploads(files)
                    jobs.append(job)

            # Aggregate results from all jobs
            created_total = sum(j.total_committed for j in jobs)
            skipped_total = sum(j.total_skipped for j in jobs)
            failed_total = sum(j.total_failed for j in jobs)

            incomplete_jobs = [
                job for job in jobs
                if job.status not in ('completed', 'completed_with_errors')
            ]
            if incomplete_jobs:
                messages.error(
                    request,
                    _('One or more import jobs did not complete. Review the job details before retrying.'),
                )
            # Build success message only after checking every job's final state.
            elif created_total == 0 and skipped_total > 0:
                messages.warning(
                    request,
                    _('No new records were created. Existing values were '\
                      'reused and %d line(s) were not valid records.')
                    % skipped_total
                )
            else:
                msg = ngettext(
                    'Successfully imported %(count)s record.',
                    'Successfully imported %(count)s records.',
                    created_total,
                ) % {'count': created_total}
                msg += ' ' + _('Filtered out %(count)s non-record lines.') % {'count': skipped_total}
                if failed_total > 0:
                    msg += ' ' + _('%(count)s record(s) failed.') % {'count': failed_total}
                messages.success(request, msg)

            # Redirect to import jobs list if multiple jobs, or job detail if single
            if len(jobs) == 1:
                return redirect('vault:import_job_detail', job_id=jobs[0].job_id)
            else:
                return redirect('vault:import_jobs')
        elif progress_request:
            return JsonResponse(
                {'error': '; '.join(
                    str(error)
                    for errors in form.errors.values()
                    for error in errors
                )},
                status=400,
            )
    else:
        form = ImportFileForm()

    return render(request, 'vault/import.html', {'form': form})


def _stream_import_progress(uploaded_files, paths, fast_mode=False):
    """Stream manifest scanning and durable import progress as NDJSON."""
    def events():
        # In production the import worker and response generator run
        # concurrently, so a bounded queue provides backpressure if the
        # client reads progress slowly. In an outer test transaction the
        # worker runs inline before events are drained; keep that test-only
        # queue unbounded to avoid blocking the same thread.
        background = not connection.in_atomic_block
        updates = queue.Queue(
            maxsize=MAX_IMPORT_PROGRESS_EVENTS if background else 0,
        )
        yield json.dumps({'type': 'phase', 'phase': 'counting'}) + '\n'

        path_groups = [
            (path, collect_files_from_path(path, ext='*'))
            for path in paths
        ]
        source_items = [
            ('path', group_index, filepath)
            for group_index, (_path, group_files) in enumerate(path_groups)
            for filepath in group_files
        ]
        source_items.extend(
            ('upload', None, uploaded_file)
            for uploaded_file in uploaded_files
        )
        total_files = len(source_items)
        if not source_items:
            yield json.dumps({
                'type': 'error',
                'message': str(_(
                    'No readable files were found in the specified path.'
                )),
            }) + '\n'
            return

        result = {
            'created': 0,
            'skipped': 0,
            'failed': 0,
            'job_ids': [],
        }
        worker_error = []

        def announce_job(job):
            job_id = str(job.job_id)
            result['job_ids'].append(job_id)
            updates.put({
                'type': 'job',
                'job_id': job_id,
                'source_name': job.source_name,
                'detail_url': reverse(
                    'vault:import_job_detail', args=[job.job_id],
                ),
                'status_url': reverse(
                    'vault:import_job_status', args=[job.job_id],
                ),
                'pause_url': reverse(
                    'vault:import_job_pause', args=[job.job_id],
                ),
                'resume_url': reverse(
                    'vault:import_job_resume', args=[job.job_id],
                ),
                'cancel_url': reverse(
                    'vault:import_job_cancel', args=[job.job_id],
                ),
            })

        def import_worker(background=True):
            if background:
                close_old_connections()
            try:
                from .import_engine import (
                    _source_manifest, _uploaded_manifest,
                    durable_import_path, durable_import_uploads,
                )

                scanned = 0

                def report_file_scanned(_source, _byte_size, _line_count):
                    nonlocal scanned
                    scanned += 1
                    updates.put({
                        'type': 'count_progress',
                        'files_scanned': scanned,
                        'total_files': total_files,
                    })

                # The view needs exact totals and the durable engine needs the
                # integrity manifest. Build both in the same read pass; the
                # engine independently revalidates path sources immediately
                # before streaming their records.
                prepared_paths = []
                group_line_counts = []
                for source_path, group_files in path_groups:
                    prepared = _source_manifest(
                        source_path,
                        ext='*',
                        files=group_files,
                        on_file_scanned=report_file_scanned,
                    )
                    prepared_paths.append(prepared)
                    group_line_counts.append(prepared[2])

                upload_manifest = (
                    _uploaded_manifest(
                        uploaded_files,
                        on_file_scanned=report_file_scanned,
                    )
                    if uploaded_files else None
                )
                upload_line_count = (
                    upload_manifest[2] if upload_manifest is not None else 0
                )
                total_lines = sum(group_line_counts) + upload_line_count
                updates.put({
                    'type': 'progress',
                    'phase': 'processing',
                    'processed': 0,
                    'total': total_lines,
                })

                context = (
                    search_indexes_suspended()
                    if fast_mode else nullcontext()
                )
                processed_before = 0
                completed_files = 0
                with context:
                    for group_number, (path, group_files) in enumerate(
                        path_groups, start=1,
                    ):
                        group_total = group_line_counts[group_number - 1]
                        prepared_manifest = prepared_paths[group_number - 1]

                        job_ref = {}

                        def path_created(job, active=job_ref):
                            active['job_id'] = str(job.job_id)
                            announce_job(job)

                        def path_progress(position, base=processed_before,
                                          active=job_ref):
                            updates.put({
                                'type': 'progress',
                                'phase': 'processing',
                                'job_id': active.get('job_id'),
                                'processed': min(total_lines, base + position),
                                'total': total_lines,
                                'files_completed': completed_files,
                                'total_files': total_files,
                            })

                        job = durable_import_path(
                            path,
                            ext='*',
                            on_progress=path_progress,
                            on_job_created=path_created,
                            batch_rows=STREAM_BATCH_SIZE,
                            expected_line_count=group_total,
                            prepared_manifest=prepared_manifest,
                        )
                        result['created'] += job.total_committed
                        result['skipped'] += job.total_skipped
                        result['failed'] += job.total_failed
                        if job.status not in (
                            'completed', 'completed_with_errors',
                        ):
                            worker_error.append('ImportIncomplete')
                        processed_before += group_total
                        completed_files += len(group_files)
                        updates.put({
                            'type': 'progress',
                            'phase': 'processing',
                            'processed': min(total_lines, processed_before),
                            'total': total_lines,
                            'files_completed': completed_files,
                            'total_files': total_files,
                        })

                    if uploaded_files:
                        job_ref = {}

                        def upload_created(job, active=job_ref):
                            active['job_id'] = str(job.job_id)
                            announce_job(job)

                        def upload_progress(position, base=processed_before,
                                            active=job_ref):
                            updates.put({
                                'type': 'progress',
                                'phase': 'processing',
                                'job_id': active.get('job_id'),
                                'processed': min(total_lines, base + position),
                                'total': total_lines,
                                'files_completed': completed_files,
                                'total_files': total_files,
                            })

                        job = durable_import_uploads(
                            uploaded_files,
                            on_progress=upload_progress,
                            on_job_created=upload_created,
                            batch_rows=STREAM_BATCH_SIZE,
                            expected_line_count=upload_line_count,
                            prepared_manifest=upload_manifest,
                        )
                        result['created'] += job.total_committed
                        result['skipped'] += job.total_skipped
                        result['failed'] += job.total_failed
                        if job.status not in (
                            'completed', 'completed_with_errors',
                        ):
                            worker_error.append('ImportIncomplete')
                        processed_before += upload_line_count
                        completed_files += len(uploaded_files)
                        updates.put({
                            'type': 'progress',
                            'phase': 'processing',
                            'processed': min(total_lines, processed_before),
                            'total': total_lines,
                            'files_completed': completed_files,
                            'total_files': total_files,
                        })
            except Exception as exc:
                # Do not include exception text: a parser/driver may include
                # the raw credential line or SQL parameters in that message.
                worker_error.append(type(exc).__name__)
            finally:
                if background:
                    connections.close_all()
                updates.put(None)

        # Django TestCase and explicit outer transactions cannot share a
        # database transaction with a worker thread. Run inline in that case
        # so test/transaction isolation is preserved; normal production
        # requests still process in the background and stream each batch.
        worker = None
        if background:
            worker = threading.Thread(
                target=import_worker,
                kwargs={'background': True},
                name='credvault-durable-import',
                daemon=True,
            )
            worker.start()
        else:
            import_worker(background=False)
        while True:
            update = updates.get()
            if update is None:
                break
            yield json.dumps(update) + '\n'
        if worker is not None:
            worker.join()

        if worker_error:
            yield json.dumps({
                'type': 'error',
                'message': str(_('Import failed; the durable job remains available for review.')),
                'error_type': worker_error[0],
            }) + '\n'
        yield json.dumps({
            'type': 'done',
            'created': result['created'],
            'skipped': result['skipped'],
            'failed': result['failed'],
            'files': total_files,
            'jobs': result['job_ids'],
            'redirect': reverse('vault:browse'),
        }) + '\n'

    response = StreamingHttpResponse(
        events(), content_type='application/x-ndjson; charset=utf-8'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def _export_account_rows(request):
    """
    Return the Account queryset the export should cover.

    When search or advanced-search filters are present in the URL they are
    applied and the *entire* matching result set in the database is exported
    (not just the current paginated page). Otherwise the entire workspace is
    exported.
    """
    params = request.GET
    if has_search_query(params):
        return filter_accounts(params)
    return Account.objects.select_related(
        'url', 'email', 'user', 'password', 'ip'
    )


def _account_csv_stream(accounts):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        _('ID'), _('URL'), _('Account Type'),
        _('Identifier'), _('Password'), _('IP address'),
        _('Created at'), _('Updated at'),
    ])
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    type_labels = {
        'email': str(_('Email')),
        'user': str(_('Username')),
    }

    rows = accounts.values_list(
        'id', 'url__name', 'account_type',
        'email__name', 'user__name', 'password__name',
        'ip__name', 'created_at', 'updated_at',
    ).iterator(chunk_size=2000)

    for (
        rec_id, url_name, account_type,
        email_name, user_name, password_name,
        ip_name, created_at, updated_at,
    ) in rows:
        ident = (
            email_name if account_type == 'email' and email_name
            else (user_name or '—')
        )
        writer.writerow([
            rec_id,
            url_name,
            type_labels.get(account_type, account_type),
            ident,
            password_name,
            ip_name if ip_name else 'N/A',
            created_at.strftime('%Y-%m-%d %H:%M') if created_at else '',
            updated_at.strftime('%Y-%m-%d %H:%M') if updated_at else '',
        ])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


def _account_json_stream(accounts):
    yield '['
    first = True
    rows = accounts.values_list(
        'id', 'url__name', 'account_type',
        'email__name', 'user__name', 'password__name',
        'ip__name', 'created_at', 'updated_at',
    ).iterator(chunk_size=2000)

    for (
        rec_id, url_name, account_type,
        email_name, user_name, password_name,
        ip_name, created_at, updated_at,
    ) in rows:
        ident = (
            email_name if account_type == 'email' and email_name
            else (user_name or '—')
        )
        data = {
            'id': rec_id,
            'url': url_name,
            'account_type': account_type,
            'identifier': ident,
            'password': password_name,
            'ip_address': ip_name if ip_name else 'N/A',
            'created_at': created_at.isoformat() if created_at else '',
            'updated_at': updated_at.isoformat() if updated_at else '',
        }
        yield ('', ',')[not first] + json.dumps(data, ensure_ascii=False)
        first = False
    yield ']'


def export_csv(request):
    """Streams the (optionally filtered) records as CSV -- safe at scale."""
    accounts = _export_account_rows(request)
    response = StreamableDownloadResponse(
        _account_csv_stream(accounts), content_type='text/csv'
    )
    response['Content-Disposition'] = (
        'attachment; filename="vault_export.csv"'
    )
    return response


def export_json(request):
    """Streams the (optionally filtered) records as JSON -- safe at scale."""
    accounts = _export_account_rows(request)
    response = StreamableDownloadResponse(
        _account_json_stream(accounts),
        content_type='application/json'
    )
    response['Content-Disposition'] = (
        'attachment; filename="vault_export.json"'
    )
    return response


# ---------------------------------------------------------------------------
# Import job management views
# ---------------------------------------------------------------------------

def _import_job_status_payload(job):
    """JSON-safe, credential-free progress and lifecycle information."""
    return {
        'job_id': str(job.job_id),
        'status': job.status,
        'status_display': str(job.get_status_display()),
        'stage': job.stage,
        'stage_display': str(job.get_stage_display()),
        'progress_percent': float(job.progress_percent),
        'total_discovered': int(job.total_discovered),
        'total_attempted': int(job.total_attempted),
        'total_committed': int(job.total_committed),
        'total_skipped': int(job.total_skipped),
        'total_failed': int(job.total_failed),
        'remaining_count': int(job.remaining_count),
        'can_pause': bool(job.can_pause()),
        'can_resume': bool(job.can_resume()),
        'can_cancel': bool(job.can_cancel()),
        'cancellation_pending': bool(
            job.status == 'cancelled' and job._has_live_lease()
        ),
    }


def import_job_status(request, job_id):
    """Return durable import progress for the live progress and detail UIs."""
    if request.method != 'GET':
        return JsonResponse({'error': 'GET required.'}, status=405)
    from .models import ImportJob

    job = get_object_or_404(ImportJob, job_id=job_id)
    response = JsonResponse(_import_job_status_payload(job))
    response['Cache-Control'] = 'no-store'
    return response


def _import_job_json_requested(request):
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('Accept', '')
    )


def _respond_import_job_action(
    request, job, *, ok, message, level='success', status=200,
):
    if _import_job_json_requested(request):
        job.refresh_from_db()
        return JsonResponse({
            'ok': bool(ok),
            'message': str(message),
            'job': _import_job_status_payload(job),
        }, status=status)

    if level == 'success':
        messages.success(request, message)
    elif level == 'error':
        messages.error(request, message)
    else:
        messages.warning(request, message)
    return redirect('vault:import_job_detail', job_id=job.job_id)


def import_job_list(request):
    """List every import job with its status and progress."""
    from .models import ImportJob

    page_obj = _paginate(
        request, ImportJob.objects.order_by('-created_at'), per_page=100,
    )
    return render(request, 'vault/import_jobs.html', {
        'jobs': page_obj.object_list,
        'page_obj': page_obj,
        'query_string': _clean_query_string(request.GET),
    })


def import_job_detail(request, job_id):
    """Show details of a specific import job."""
    from .models import ImportJob

    job = get_object_or_404(ImportJob, job_id=job_id)
    recent_failures = job.records.filter(status='failed').order_by('-processed_at')[:20]

    return render(request, 'vault/import_job_detail.html', {
        'job': job,
        'recent_failures': recent_failures,
    })


def import_job_resume(request, job_id):
    """Continue a live pause or recover an abandoned replayable source."""
    from .models import ImportJob
    from .import_engine import lines_factory_for_job, recover_job

    job = get_object_or_404(ImportJob, job_id=job_id)
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'error': 'GET or POST required.'}, status=405)
    if not job.can_resume():
        message = _('This job cannot be resumed from its available source.')
        if request.method == 'POST':
            return _respond_import_job_action(
                request, job, ok=False, message=message,
                level='warning', status=409,
            )
        messages.error(request, message)
        return redirect('vault:import_job_detail', job_id=job.job_id)

    if request.method == 'POST':
        if job.status == 'paused' and job.mark_resumed():
            return _respond_import_job_action(
                request, job, ok=True,
                message=_('Import continued.'),
            )
        if job.status == 'paused':
            job.refresh_from_db()
            if job.status != 'paused':
                return _respond_import_job_action(
                    request, job, ok=False,
                    message=_('The job changed state before it could continue.'),
                    level='warning', status=409,
                )

        # An expired/unowned path-backed job has no live request worker to
        # continue, so reconcile and replay only its validated source gaps.
        recovery_log = recover_job(
            job, lines_iterable_factory=lines_factory_for_job(job),
        )
        if recovery_log.outcome == 'success':
            return _respond_import_job_action(
                request, job, ok=True,
                message=_('Job recovery completed successfully.'),
            )
        if recovery_log.outcome == 'partial':
            return _respond_import_job_action(
                request, job, ok=True,
                message=recovery_log.notes,
                level='warning',
            )
        return _respond_import_job_action(
            request, job, ok=False,
            message=recovery_log.notes,
            level='error', status=409,
        )

    return render(request, 'vault/import_job_confirm.html', {
        'job': job,
        'action': 'resume',
    })


def import_job_cancel(request, job_id):
    """Persist cancellation and let a live worker stop at its next batch."""
    from .models import ImportJob

    job = get_object_or_404(ImportJob, job_id=job_id)
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'error': 'GET or POST required.'}, status=405)
    if not job.can_cancel():
        message = _('This job cannot be cancelled.')
        if request.method == 'POST':
            return _respond_import_job_action(
                request, job, ok=False, message=message,
                level='warning', status=409,
            )
        messages.error(request, message)
        return redirect('vault:import_job_detail', job_id=job.job_id)

    if request.method == 'POST':
        if job.mark_cancelled():
            return _respond_import_job_action(
                request, job, ok=True,
                message=_('Cancellation requested; the current durable batch will finish.'),
            )
        return _respond_import_job_action(
            request, job, ok=False,
            message=_('The job changed state before it could be cancelled.'),
            level='warning', status=409,
        )

    return render(request, 'vault/import_job_confirm.html', {
        'job': job,
        'action': 'cancel',
    })


def import_job_pause(request, job_id):
    """Persist a cooperative pause request for the current live worker."""
    from .models import ImportJob

    job = get_object_or_404(ImportJob, job_id=job_id)
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'error': 'GET or POST required.'}, status=405)
    if not job.can_pause():
        message = _('This job cannot be paused.')
        if request.method == 'POST':
            return _respond_import_job_action(
                request, job, ok=False, message=message,
                level='warning', status=409,
            )
        messages.error(request, message)
        return redirect('vault:import_job_detail', job_id=job.job_id)

    if request.method == 'POST':
        if job.mark_paused():
            return _respond_import_job_action(
                request, job, ok=True,
                message=_('Import paused at a durable batch boundary.'),
            )
        return _respond_import_job_action(
            request, job, ok=False,
            message=_('The job changed state before it could be paused.'),
            level='warning', status=409,
        )

    return render(request, 'vault/import_job_confirm.html', {
        'job': job,
        'action': 'pause',
    })


def import_job_verify(request, job_id):
    """Verify the integrity of an import job."""
    from .models import ImportJob
    from .import_engine import verify_import_job

    job = get_object_or_404(ImportJob, job_id=job_id)
    results = verify_import_job(job)

    return render(request, 'vault/import_job_verify.html', {
        'job': job,
        'results': results,
    })


def import_job_records(request, job_id):
    """List all records for an import job with filtering and pagination."""
    from .models import ImportJob

    job = get_object_or_404(ImportJob, job_id=job_id)

    status_filter = request.GET.get('status', '')
    records = job.records.all()
    if status_filter:
        records = records.filter(status=status_filter)

    page_obj = _paginate(request, records, per_page=100)
    return render(request, 'vault/import_job_records.html', {
        'job': job,
        'page_obj': page_obj,
        'status_filter': status_filter,
        'query_string': _clean_query_string(request.GET),
    })
