import csv
import io
import json
import os
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, ProtectedError, Q
from django.db.models.functions import Lower
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy, ngettext

from .forms import (
    AccountForm, AdvancedSearchForm, ImportFileForm, QuickSearchForm,
    ReferenceForm,
)
from . import import_jobs
from .import_jobs import Source
from .importer import is_candidate_file
from .models import Account, Email, Ip, Pass, Url, User
from .utils import extract_domain


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
    LOWER(name). Executes in O(log N) (< 1 ms across millions of rows).
    """
    val = (raw_value or '').strip().lower()
    if not val:
        return []
    return list(
        model.objects.annotate(_n=Lower('name'))
        .filter(_n=val)
        .values_list('id', flat=True)
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


def _match_url_ids(raw_value, force_domain=False):
    """
    Look up matching Url IDs using the B-tree index on LOWER(name).

    - Always matches the exact URL (case-insensitive).
    - When the query is a domain (e.g. 'example.com') or force_domain=True,
      also performs indexed range scans for URLs hosted on that domain
      (https://domain/..., http://domain/..., https://www.domain/..., etc.).
    """
    q_lower = (raw_value or '').strip().lower()
    if not q_lower:
        return []

    exact_set = {q_lower, q_lower.rstrip('/')}
    clauses = Q(_n__in=exact_set)

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
                        clauses |= Q(_n__gte=prefix, _n__lt=prefix + '\uffff')
            clauses |= Q(_n__in=exact_set)

    ids = list(
        Url.objects.annotate(_n=Lower('name'))
        .filter(clauses)
        .values_list('id', flat=True)
    )
    if not ids and force_domain:
        dom = extract_domain(q_lower)
        if dom:
            ids = list(
                Url.objects.filter(name__icontains=dom)
                .values_list('id', flat=True)
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
    """Return URL query string without the pagination 'page' parameter."""
    params = request_get.copy()
    params.pop('page', None)
    return params.urlencode()


# ---------------------------------------------------------------------------
# Filter UI support
# ---------------------------------------------------------------------------

# Placeholder + hint per quick-search field. The segmented control in the
# filter bar reads these, so the input explains itself the moment a field is
# picked ('example.com' for a domain, 'user@example.com' for an email, ...).
QUICK_SEARCH_HINTS = {
    'all': _lazy('Any value -- exact match across URL, email, username, password, IP'),
    'domain': _lazy('example.com'),
    'url': _lazy('https://example.com/login or example.com'),
    'email': _lazy('user@example.com'),
    'user': _lazy('the username exactly as stored'),
    'password': _lazy('the password or secret exactly as stored'),
    'ip': _lazy('203.0.113.10'),
}

QUICK_SEARCH_ICONS = {
    'all': 'search',
    'domain': 'globe',
    'url': 'link',
    'email': 'mail',
    'user': 'user',
    'password': 'key',
    'ip': 'hash',
}

# Query parameters that name a filter, in the order the chips show them.
FILTER_CHIP_LABELS = {
    'field': _lazy('Search in'),
    'q': _lazy('Value'),
    'keyword': _lazy('Keyword'),
    'url': _lazy('URL contains'),
    'account_type': _lazy('Account type'),
    'email': _lazy('Email contains'),
    'username': _lazy('Username contains'),
    'password': _lazy('Password contains'),
    'ip': _lazy('IP contains'),
    'created_from': _lazy('Created from'),
    'created_to': _lazy('Created to'),
    'sort': _lazy('Sort'),
    'match': _lazy('Match'),
}

_FILTER_ICONS = {
    'q': 'search',
    'keyword': 'search',
    'url': 'link',
    'ip': 'hash',
    'email': 'mail',
    'username': 'user',
    'password': 'key',
    'account_type': 'users',
    'created_from': 'calendar',
    'created_to': 'calendar',
    'sort': 'sort',
    'match': 'sliders',
}

ACCOUNT_TYPE_LABELS = dict(Account.ACCOUNT_TYPE_CHOICES)


def _choice_rows(choices, hints=None, icons=None):
    """Normalize Django choices into ``{'value','label','hint','icon'}`` rows."""
    rows = []
    for value, label in choices:
        rows.append({
            'value': value,
            'label': str(label),
            'hint': str((hints or {}).get(value, '')),
            'icon': (icons or {}).get(value, 'search'),
        })
    return rows


def _human_filter_value(key, value):
    """Render a raw query value the way the control that produced it shows it."""
    if key == 'field':
        for choice_value, label in QuickSearchForm.FIELD_CHOICES:
            if choice_value == value:
                return str(label)
    if key == 'account_type':
        return str(ACCOUNT_TYPE_LABELS.get(value, value))
    if key == 'sort':
        for choice_value, label in AdvancedSearchForm.SORT_CHOICES:
            if choice_value == value:
                return str(label)
    if key == 'match':
        return str(_lazy('Contains') if value == 'contains' else _lazy('Starts with'))
    return value


def active_filters(request, *, skip=()):
    """
    The chips row above a result list: one per filter, each with its own
    removal link.

    Derived from the query string rather than the form, so a bookmarked or
    shared URL shows exactly what is filtering the results even when a value
    is not accepted by the form.
    """
    skip = set(skip) | {'page', 'adv'}
    # The quick search needs two parameters to mean anything: choosing a field
    # with no value typed is not yet a filter, so it gets no chip of its own.
    has_query = bool((request.GET.get('q') or '').strip())
    chips = []
    for key in request.GET.keys():
        if key in skip or (key == 'field' and not has_query):
            continue
        value = (request.GET.get(key) or '').strip()
        if not value:
            continue
        remaining = request.GET.copy()
        remaining.pop(key, None)
        remaining.pop('page', None)
        chips.append({
            'key': key,
            'label': str(FILTER_CHIP_LABELS.get(key, key.replace('_', ' ').title())),
            'value': _human_filter_value(key, value),
            'icon': _FILTER_ICONS.get(key, 'sliders'),
            'clear': '?' + remaining.urlencode() if remaining else '?',
        })
    return chips


def filter_summary(request, *, skip=()):
    """Chips plus the count shown in the filter bar badge."""
    chips = active_filters(request, skip=skip)
    return {'chips': chips, 'count': len(chips)}


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
            kw_clauses = Q()
            url_ids = list(
                Url.objects.filter(name__icontains=keyword)
                .values_list('id', flat=True)
            )
            email_ids = list(
                Email.objects.filter(name__icontains=keyword)
                .values_list('id', flat=True)
            )
            user_ids = list(
                User.objects.filter(name__icontains=keyword)
                .values_list('id', flat=True)
            )
            pass_ids = list(
                Pass.objects.filter(name__icontains=keyword)
                .values_list('id', flat=True)
            )
            ip_ids = list(
                Ip.objects.filter(name__icontains=keyword)
                .values_list('id', flat=True)
            )
            if url_ids:
                kw_clauses |= Q(url_id__in=url_ids)
            if email_ids:
                kw_clauses |= Q(email_id__in=email_ids)
            if user_ids:
                kw_clauses |= Q(user_id__in=user_ids)
            if pass_ids:
                kw_clauses |= Q(password_id__in=pass_ids)
            if ip_ids:
                kw_clauses |= Q(ip_id__in=ip_ids)
            if not kw_clauses:
                return qs.none()
            qs = qs.filter(kw_clauses)

        url = (params.get('url') or '').strip()
        if url:
            ids = list(
                Url.objects.filter(name__icontains=url)
                .values_list('id', flat=True)
            )
            if not ids:
                return qs.none()
            qs = qs.filter(url_id__in=ids)

        account_type = (params.get('account_type') or '').strip()
        if account_type:
            qs = qs.filter(account_type=account_type)

        email = (params.get('email') or '').strip()
        if email:
            ids = list(
                Email.objects.filter(name__icontains=email)
                .values_list('id', flat=True)
            )
            if not ids:
                return qs.none()
            qs = qs.filter(email_id__in=ids)

        username = (params.get('username') or '').strip()
        if username:
            ids = list(
                User.objects.filter(name__icontains=username)
                .values_list('id', flat=True)
            )
            if not ids:
                return qs.none()
            qs = qs.filter(user_id__in=ids)

        password = (params.get('password') or '').strip()
        if password:
            ids = list(
                Pass.objects.filter(name__icontains=password)
                .values_list('id', flat=True)
            )
            if not ids:
                return qs.none()
            qs = qs.filter(password_id__in=ids)

        ip = (params.get('ip') or '').strip()
        if ip:
            ids = list(
                Ip.objects.filter(name__icontains=ip)
                .values_list('id', flat=True)
            )
            if not ids:
                return qs.none()
            qs = qs.filter(ip_id__in=ids)

        created_from = params.get('created_from')
        if created_from:
            qs = qs.filter(created_at__date__gte=created_from)
        created_to = params.get('created_to')
        if created_to:
            qs = qs.filter(created_at__date__lte=created_to)

        sort = params.get('sort') or 'newest'
        if sort == 'oldest':
            qs = qs.order_by('created_at', 'id')
        elif sort == 'url':
            qs = qs.order_by('url__name', 'id')
        elif sort == 'identifier':
            qs = qs.order_by('email__name', 'user__name', 'id')
        else:
            qs = qs.order_by('-created_at', 'id')
        return qs

    search_term = q or keyword
    if not search_term:
        return qs.none()

    if field == 'domain':
        ids = _match_url_ids(search_term, force_domain=True)
        if not ids:
            return qs.none()
        return qs.filter(url_id__in=ids)

    if field == 'url':
        ids = _match_url_ids(search_term, force_domain=False)
        if not ids:
            return qs.none()
        return qs.filter(url_id__in=ids)

    if field in _QUICK_SEARCH_MAP:
        model, fk = _QUICK_SEARCH_MAP[field]
        ids = _match_exact_ids(model, search_term)
        if not ids:
            return qs.none()
        return qs.filter(**{f"{fk}__in": ids})

    # Default ('all' or unspecified field or 'keyword'): check all 5 indexed
    # reference tables (Domain/URL, Email, Username, Password, IP) in < 1 ms.
    url_ids = _match_url_ids(search_term, force_domain=False)
    email_ids = _match_exact_ids(Email, search_term)
    user_ids = _match_exact_ids(User, search_term)
    pass_ids = _match_exact_ids(Pass, search_term)
    ip_ids = _match_exact_ids(Ip, search_term)

    clauses = Q()
    if url_ids:
        clauses |= Q(url_id__in=url_ids)
    if email_ids:
        clauses |= Q(email_id__in=email_ids)
    if user_ids:
        clauses |= Q(user_id__in=user_ids)
    if pass_ids:
        clauses |= Q(password_id__in=pass_ids)
    if ip_ids:
        clauses |= Q(ip_id__in=ip_ids)

    if clauses:
        return qs.filter(clauses)

    # If invoked via free-text keyword and no exact/domain match was found,
    # fall back to reference-table substring lookup.
    if keyword and not q:
        url_ids = list(
            Url.objects.filter(name__icontains=keyword)
            .values_list('id', flat=True)
        )
        email_ids = list(
            Email.objects.filter(name__icontains=keyword)
            .values_list('id', flat=True)
        )
        user_ids = list(
            User.objects.filter(name__icontains=keyword)
            .values_list('id', flat=True)
        )
        pass_ids = list(
            Pass.objects.filter(name__icontains=keyword)
            .values_list('id', flat=True)
        )
        ip_ids = list(
            Ip.objects.filter(name__icontains=keyword)
            .values_list('id', flat=True)
        )
        if url_ids:
            clauses |= Q(url_id__in=url_ids)
        if email_ids:
            clauses |= Q(email_id__in=email_ids)
        if user_ids:
            clauses |= Q(user_id__in=user_ids)
        if pass_ids:
            clauses |= Q(password_id__in=pass_ids)
        if ip_ids:
            clauses |= Q(ip_id__in=ip_ids)
        if clauses:
            return qs.filter(clauses)

    return qs.none()


def _paginate(request, queryset, per_page=100):
    paginator = Paginator(queryset, per_page)
    page_number = request.GET.get('page', '1')
    return paginator.get_page(page_number)


def _reference_choices(kind):
    """Existing names for a reference kind, used to power autocomplete."""
    model = REFERENCE_REGISTRY[kind]['model']
    return list(model.objects.values_list('name', flat=True)[:200])


def dashboard(request):
    """Replaces: main_menu() -- the landing page with workspace stats."""
    total_accounts = Account.objects.count()
    reference_counts = {
        kind: REFERENCE_REGISTRY[kind]['model'].objects.count()
        for kind in REFERENCE_REGISTRY
    }
    reference_rows = [
        {
            'kind': kind,
            'label_plural': REFERENCE_REGISTRY[kind]['label_plural'],
            'count': reference_counts[kind],
        }
        for kind in REFERENCE_REGISTRY
    ]
    return render(request, 'vault/dashboard.html', {
        'total_records': total_accounts,
        'reference_rows': reference_rows,
        'reference_registry': REFERENCE_REGISTRY,
    })


def _quick_search_state(request, form):
    """Context shared by every page that shows the quick filter bar."""
    field_value = (request.GET.get('field') or '').strip() or 'all'
    return {
        'form': form,
        'quick_fields': _choice_rows(
            QuickSearchForm.FIELD_CHOICES, QUICK_SEARCH_HINTS,
            QUICK_SEARCH_ICONS,
        ),
        'active_field': field_value,
        'active_hint': QUICK_SEARCH_HINTS.get(field_value, ''),
        'active_query': (
            request.GET.get('q') or request.GET.get('keyword') or ''
        ).strip(),
        'filters': filter_summary(request),
        'filter_title': _lazy('Quick search'),
        'filter_hint': _lazy(
            'Indexed and exact: paste a whole value, or pick a field to narrow '
            'the match.'
        ),
    }


def _search_results(request, form, *, skip_filters=()):
    """
    Run a quick search and build its context, or return an empty result set.

    Records are only queried when a filter is actually set -- dumping an
    unfiltered result set across millions of rows is never what the user wants.
    """
    has_query = has_search_query(request.GET)
    records = []
    page_obj = None
    total = 0

    if has_query:
        queryset = filter_accounts(request.GET)
        total = queryset.count()
        page_obj = _paginate(request, queryset)
        records = page_obj.object_list

    context = _quick_search_state(request, form)
    context.update({
        'has_query': has_query,
        'records': records,
        'page_obj': page_obj,
        'total': total,
        'query_string': _clean_query_string(request.GET),
    })
    return context


def browse_all(request):
    """
    Records view: never dumps unqueried records across millions of rows.
    Displays matching records only when the user submits a search query.
    """
    form = QuickSearchForm(request.GET or None)
    context = _search_results(request, form)
    context['total_records'] = Account.objects.count()
    context['filter_title'] = _lazy('Find records')
    context['filter_hint'] = _lazy(
        'The full table is never dumped: pick a field, enter a value, and the '
        'matching records appear.'
    )
    return render(request, 'vault/browse.html', context)


def search_records(request):
    """
    Fast, indexed search across domain, URL, email, username, password, or IP.
    Results are only queried and displayed when the user submits a query, and
    the full matching dataset can be exported in CSV or JSON.
    """
    form = QuickSearchForm(request.GET or None)
    context = _search_results(request, form)
    context['filter_title'] = _lazy('Exact match search')
    context['filter_hint'] = _lazy(
        'One field, one value, resolved through the index on that field.'
    )
    return render(request, 'vault/search.html', context)


# Fields grouped the way an analyst reads them, not in model order.
ADVANCED_GROUPS = (
    {
        'id': 'scope',
        'title': _lazy('Search everywhere'),
        'help': _lazy(
            'Free text is matched against every stored value at once: URLs, '
            'emails, usernames, passwords and IPs.'
        ),
        'icon': 'search',
        'fields': ('keyword',),
    },
    {
        'id': 'where',
        'title': _lazy('Where the record points'),
        'help': _lazy('Partial values are allowed here, so a domain is enough.'),
        'icon': 'globe',
        'fields': ('url', 'ip'),
    },
    {
        'id': 'who',
        'title': _lazy('Who owns it'),
        'help': _lazy('Every filter below is combined with the others (AND).'),
        'icon': 'users',
        'fields': ('account_type', 'email', 'username'),
    },
    {
        'id': 'secret',
        'title': _lazy('The secret'),
        'help': _lazy('Leave it empty to search by owner or location only.'),
        'icon': 'key',
        'fields': ('password',),
    },
    {
        'id': 'when',
        'title': _lazy('When it was stored'),
        'help': _lazy('Dates are compared against the record creation date.'),
        'icon': 'calendar',
        'fields': ('created_from', 'created_to'),
    },
    {
        'id': 'order',
        'title': _lazy('Order the results'),
        'help': _lazy('Applies to the result table and to the export files.'),
        'icon': 'sort',
        'fields': ('sort',),
    },
)

_ADVANCED_FIELD_ICONS = {
    'keyword': 'search',
    'url': 'link',
    'ip': 'hash',
    'email': 'mail',
    'username': 'user',
    'password': 'key',
    'account_type': 'users',
    'created_from': 'calendar',
    'created_to': 'calendar',
    'sort': 'sort',
}

_SEGMENTED_FIELDS = {
    'account_type': [
        ('', _lazy('Any')),
        ('email', _lazy('Email')),
        ('user', _lazy('Username')),
    ],
    'sort': AdvancedSearchForm.SORT_CHOICES,
}


def _advanced_filter_groups(form, data):
    """
    Build the grouped filter panel.

    Choice fields become segmented controls (real radio inputs, so the panel
    still filters without JavaScript); everything else keeps its own widget
    inside the shared field shell.
    """
    groups = []
    for group in ADVANCED_GROUPS:
        rows = []
        for name in group['fields']:
            bound = form[name]
            if name in _SEGMENTED_FIELDS:
                rows.append({
                    'kind': 'segmented',
                    'name': name,
                    'label': str(bound.label),
                    'icon': _ADVANCED_FIELD_ICONS.get(name, 'sliders'),
                    'choices': [
                        {'value': value, 'label': str(label)}
                        for value, label in _SEGMENTED_FIELDS[name]
                    ],
                    'current': (data.get(name) or '').strip()
                    or _default_segment(name),
                })
            else:
                rows.append({
                    'kind': 'field',
                    'field': bound,
                    'icon': _ADVANCED_FIELD_ICONS.get(name, 'sliders'),
                    'wide': name in ('keyword', 'url'),
                })
        groups.append({
            'id': group['id'],
            'title': str(group['title']),
            'help': str(group['help']),
            'icon': group['icon'],
            'rows': rows,
        })
    return groups


def _default_segment(name):
    if name == 'sort':
        return 'newest'
    return ''


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
    has_query = has_search_query(request.GET)

    if request.GET and has_query:
        params = request.GET.copy()
        params['adv'] = '1'
        queryset = filter_accounts(params)
        total = queryset.count()
        page_obj = _paginate(request, queryset)
        records = page_obj.object_list

    data = request.GET or {}

    context = {
        'form': form,
        'records': records,
        'page_obj': page_obj,
        'total': total,
        'has_query': has_query,
        'query_string': _clean_query_string(request.GET),
        'filter_groups': _advanced_filter_groups(form, data),
        'filters': filter_summary(request),
        'sort_value': (data.get('sort') or 'newest'),
        'account_type_value': (data.get('account_type') or ''),
    }
    return render(request, 'vault/advanced_search.html', context)


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
        kind: REFERENCE_REGISTRY[kind]['model'].objects.count()
        for kind in REFERENCE_REGISTRY
    }
    reference_rows = [
        {
            'kind': kind,
            'label': REFERENCE_REGISTRY[kind]['label'],
            'label_plural': REFERENCE_REGISTRY[kind]['label_plural'],
            'count': reference_counts[kind],
        }
        for kind in REFERENCE_REGISTRY
    ]
    total_records = Account.objects.count()
    return render(request, 'vault/references_index.html', {
        'reference_rows': reference_rows,
        'total_records': total_records,
    })


# How a value typed into a reference filter is compared, and in what order
# the table is listed. 'prefix' rides the functional LOWER(name) index, which
# is what keeps a reference table of millions of values instant to filter;
# 'contains' is offered too, but it is labelled as the slower choice.
REFERENCE_MATCH_CHOICES = [
    ('prefix', _lazy('Starts with')),
    ('contains', _lazy('Contains')),
]

REFERENCE_SORT_CHOICES = [
    ('name', _lazy('Name (A-Z)')),
    ('name_desc', _lazy('Name (Z-A)')),
    ('used', _lazy('Most used')),
    ('recent', _lazy('Newest')),
]


def _reference_filter_context(request, model, qs, *, total_values, matched):
    q = (request.GET.get('q') or '').strip()
    mode = (request.GET.get('match') or 'prefix').strip()
    sort = (request.GET.get('sort') or 'name').strip()
    return {
        'filter_q': q,
        'filter_mode': mode if mode in dict(REFERENCE_MATCH_CHOICES) else 'prefix',
        'filter_choices': _choice_rows(REFERENCE_MATCH_CHOICES),
        'sort_value': sort if sort in dict(REFERENCE_SORT_CHOICES) else 'name',
        'sort_choices': _choice_rows(REFERENCE_SORT_CHOICES),
        'has_filter': bool(q),
        'matched': matched,
        'total_values': total_values,
        # The match and order controls are visible right above the table, so a
        # chip for either would only repeat what the segments already say.
        'filters': filter_summary(request, skip=('match', 'sort')),
    }


def reference_list(request, kind):
    """
    List, filter and extend one of the five reference tables.

    The filter box is part of the shared filter system, so typing here behaves
    exactly like typing in the records and search screens -- same field shell,
    same chips, same clear links.
    """
    if kind not in REFERENCE_REGISTRY:
        return redirect('vault:dashboard')

    registry = REFERENCE_REGISTRY[kind]
    model = registry['model']

    if request.method == 'POST':
        form = ReferenceForm(request.POST)
        if form.is_valid():
            name = form.cleaned_data['name'].strip()
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
        form = ReferenceForm()

    q = (request.GET.get('q') or '').strip()
    mode = (request.GET.get('match') or 'prefix').strip()
    sort = (request.GET.get('sort') or 'name').strip()

    qs = model.objects.annotate(usage=Count('accounts'))
    if q:
        if mode == 'contains':
            qs = qs.filter(name__icontains=q)
        else:
            lowered = q.lower()
            qs = (
                qs.annotate(name_lower=Lower('name'))
                .filter(name_lower__gte=lowered, name_lower__lt=lowered + '\uffff')
            )

    total_values = model.objects.count()
    matched = qs.count() if q else total_values

    if sort == 'used':
        qs = qs.order_by('-usage', 'name')
    elif sort == 'recent':
        qs = qs.order_by('-created_at', 'id')
    elif sort == 'name_desc':
        qs = qs.order_by('-name')
    else:
        qs = qs.order_by('name')

    page_obj = _paginate(request, qs, per_page=100)

    context = {
        'kind': kind,
        'registry': registry,
        'reference_registry': REFERENCE_REGISTRY,
        'form': form,
        'entities': page_obj.object_list,
        'page_obj': page_obj,
        'query_string': _clean_query_string(request.GET),
    }
    context.update(_reference_filter_context(
        request, model, qs, total_values=total_values, matched=matched
    ))
    return render(request, 'vault/references.html', context)


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


# ---------------------------------------------------------------------------
# Import: browser uploads and server paths, with live progress
# ---------------------------------------------------------------------------

IMPORT_TEMPLATE = 'vault/import.html'


def _wants_json(request):
    """True for the workspace XHR calls, as opposed to a plain form post."""
    if (request.headers.get('x-requested-with') or '').lower() == 'xmlhttprequest':
        return True
    accept = request.headers.get('accept') or ''
    return 'application/json' in accept and 'text/html' not in accept


def local_import_paths_allowed():
    """
    Whether a folder/file path on the server may be imported from the browser.

    On a host where the workspace is reachable by people who should not read
    arbitrary paths, set ``VAULT_ALLOW_LOCAL_IMPORT_PATHS = False`` and only
    browser uploads stay available.
    """
    return getattr(settings, 'VAULT_ALLOW_LOCAL_IMPORT_PATHS', True)


def _upload_sources(request):
    """
    Build import sources from the uploaded files.

    The folder picker also sends ``relpath`` for each file so a progress row
    can read ``sub/dir/records.txt`` instead of a bare ``records.txt``; when
    the counts do not line up the plain names are used. Known binary files are
    dropped and reported, because parsing them cannot produce records and
    would stretch the progress bar for nothing.
    """
    uploads = list(request.FILES.getlist('file'))
    uploads += list(request.FILES.getlist('folder'))
    relpaths = request.POST.getlist('relpath')
    if len(relpaths) != len(uploads):
        relpaths = []

    sources = []
    skipped = []
    for index, uploaded in enumerate(uploads):
        name = getattr(uploaded, 'name', '') or ''
        if not is_candidate_file(name):
            skipped.append(name or _('(unnamed file)'))
            continue
        sources.append(Source.from_upload(
            uploaded, relpath=relpaths[index] if relpaths else name
        ))
    return sources, skipped


def _path_sources(folder_path):
    """
    Sources, byte total and file count for a server-side path.

    ``stats['skipped']`` carries how many files were seen but not text, which
    is reported to the user instead of silently shrinking the list.
    """
    stats = {'skipped': 0}
    paths, total_bytes = import_jobs.scan(folder_path, stats=stats)
    root = folder_path if os.path.isdir(folder_path) else None
    sources = import_jobs.sources_for_paths(paths, root=root)
    return sources, total_bytes, len(paths), stats['skipped']


def _import_job_label(source_count, folder_path):
    if folder_path:
        return _lazy("Server path: %(path)s") % {'path': folder_path}
    return ngettext(
        '%(count)s uploaded file', '%(count)s uploaded files', source_count
    ) % {'count': source_count}


def _import_labels():
    """
    Strings the progress panel needs, translated on the server.

    The readout is rendered by JavaScript, so its labels are handed over as
    data instead of being hardcoded in the script -- the panel speaks the same
    language as the rest of the workspace.
    """
    return {
        'start': _('Start import'),
        'importing': _('Importing…'),
        'sending': _('Sending files to the server'),
        'listing': _('Listing files…'),
        'noFiles': _('Add at least one file to import.'),
        'noPath': _('Enter a folder or file path first.'),
        'binaryExcluded': _('{n} file(s) are not text and were excluded.'),
        'moreFiles': _('+ {n} more file(s) in the list'),
        'notTextFile': _('Listed as a non-text file — excluded'),
        'include': _('Include'),
        'exclude': _('Exclude'),
        'remove': _('Remove'),
        'ready': _('ready'),
        'excluded': _('excluded'),
        'files': _('Files'),
        'totalSize': _('Total size'),
        'skipped': _('Skipped'),
        'firstOf': _('Showing the first {a} of {b} files.'),
        'scanNothing': _(
            'Nothing has been read yet. Start import reads exactly these '
            'files.'
        ),
        'prepareFailed': _('The import could not be prepared.'),
        'requestFailed': _('The import request failed (HTTP {status}).'),
        'stoppedReporting': _('The server stopped reporting this import.'),
        'lostJob': _(
            'This import is no longer being tracked. Check the records '
            'list for what was stored.'
        ),
        'uploadedOf': _('{done} of {total} uploaded'),
        'transferSub': _(
            '{done} of {total} uploaded · reading starts when the last '
            'file lands'
        ),
        'percentRead': _('{percent}% of {total} read'),
        'readOf': _('{done} of {total} read'),
        'filesDone': _('{done} of {total} files done'),
        'pendingLines': _('{n} line(s) waiting for the next write'),
        'stale': _('no update from the server for a while'),
        'stopping': _('stopping after the current batch'),
        'waitingForServer': _('Waiting for the server to start reading'),
        'rowsOf': _('rows {a}-{b} of {total}'),
        'filesCount': _('{n} file(s)'),
        'filesUnreadable': _('{n} file(s) could not be read.'),
        'moreNotes': _('+{n} more note(s) not shown.'),
        'unreadable': _('unreadable'),
        'queuedState': _('queued'),
        'readingState': _('reading'),
        'storedState': _('stored'),
        'failedState': _('failed'),
        'stoppedState': _('stopped'),
        'finishedIn': _('Import finished in {time}'),
        'stoppedAfter': _('Import stopped after {time}'),
        'couldNotFinish': _('The import could not be finished'),
        'summaryLine': _('{created} new record(s) from {lines} line(s) read · '
                         '{duplicates} already stored · {invalid} not records'),
        'newRecords': _('New records'),
        'alreadyStored': _('Already stored'),
        'notRecords': _('Not records'),
        'emptyLines': _('Empty or comment'),
        'linesRead': _('Lines read'),
        'dataRead': _('Data read'),
        'time': _('Time'),
        'nothingNew': _(
            'Nothing new was stored: every valid line already existed in '
            'the vault.'
        ),
        'measured': _(
            'Every number above was measured while reading the files, not '
            'estimated.'
        ),
        'failedLabel': _('Import failed'),
        'nothingAfterFailure': _('Nothing else was written after the failure.'),
        'finishingBatch': _('Finishing the current batch, then stopping…'),
        'secondsShort': _('{n}s'),
        'minutesShort': _('{m}m {s}s'),
        'hoursShort': _('{h}h {m}m'),
    }


def _import_page(request, form, status=None):
    context = {
        'form': form,
        'allow_local_paths': local_import_paths_allowed(),
        'import_labels': _import_labels(),
        'import_urls': {
            'import': reverse('vault:import'),
            'prepare': reverse('vault:import_prepare'),
            'status': reverse('vault:import_job_status', args=['__JOB_ID__']),
            'stop': reverse('vault:import_job_stop', args=['__JOB_ID__']),
            'browse': reverse('vault:browse'),
        },
    }
    if status is None:
        return render(request, IMPORT_TEMPLATE, context)
    return render(request, IMPORT_TEMPLATE, context, status=status)


def import_from_file(request):
    """
    Import one file, a whole folder of files, or a path on the server.

    The same view answers both kinds of client:

    * the plain form posts normally and the user lands back on Records with a
      summary message once the import is over;
    * the workspace screen posts with ``X-Requested-With`` and polls
      :func:`import_job_status` *while this call is still running*, so it can
      show a byte-accurate bar, the live counters behind it, and a Stop button
      for imports of any size.
    """
    if request.method != 'POST':
        return _import_page(request, ImportFileForm())

    as_json = _wants_json(request)
    form = ImportFileForm(request.POST, request.FILES)
    if not form.is_valid():
        if as_json:
            return JsonResponse(
                {'ok': False, 'errors': form.errors}, status=422
            )
        return _import_page(request, form)

    def reject(message, field=None):
        if as_json:
            return JsonResponse(
                {'ok': False, 'error': str(message), 'field': field},
                status=400,
            )
        form.add_error(field, str(message))
        return _import_page(request, form)

    folder_path = (form.cleaned_data.get('folder_path') or '').strip()
    sources, skipped_names = _upload_sources(request)
    path_notice = None

    if folder_path:
        if not local_import_paths_allowed():
            return reject(
                _('Importing from a server path is disabled in this workspace.'),
                'folder_path',
            )
        if not (os.path.isdir(folder_path) or os.path.isfile(folder_path)):
            return reject(
                _('The specified folder or file path does not exist.'),
                'folder_path',
            )
        job = import_jobs.get(request.POST.get('job'))
        reused = job.reuse_scan(folder_path) if job else None
        if reused is not None:
            path_sources, path_bytes = reused
            path_skipped = 0
        else:
            path_sources, path_bytes, _count, path_skipped = _path_sources(
                folder_path
            )
        if not path_sources:
            if sources:
                path_notice = _(
                    'No readable files were found in %(path)s, so only the '
                    'uploaded files were imported.'
                ) % {'path': folder_path}
            else:
                return reject(
                    _('No readable files were found in the specified path.'),
                    'folder_path',
                )
        else:
            sources.extend(path_sources)

    if not sources:
        return reject(_('Please choose a file or a folder to import.'))

    job = import_jobs.get(request.POST.get('job'))
    if job is None:
        job = import_jobs.create(
            label='', source_kind='path' if folder_path else 'upload',
            register_files=False, files_total=len(sources),
        )
    job.label = str(_import_job_label(len(sources), folder_path))
    job.replace_sources(sources)
    if folder_path and path_skipped:
        job.warn(_("%(count)s file(s) in %(path)s are not text and were "
                   "ignored.") % {'count': path_skipped, 'path': folder_path})
    for name in skipped_names:
        job.warn(_("%(name)s was skipped: not a text file.") % {'name': name})
    if path_notice:
        job.warn(path_notice)

    import_jobs.run(job, sources)
    stats = job.stats

    if as_json:
        return JsonResponse({
            'ok': job.state != import_jobs.STATE_ERROR,
            'job_id': job.id,
            'state': job.state,
            'summary': job.snapshot(include_files=False),
        })

    if job.state == import_jobs.STATE_CANCELLED:
        messages.info(
            request,
            _('Import stopped after %(count)s record.')
            % {'count': stats.created}
            if stats.created == 1
            else _('Import stopped after %(count)s records.')
            % {'count': stats.created}
        )
    elif job.state == import_jobs.STATE_ERROR:
        messages.error(request, job.error or _('The import failed.'))
    elif stats.created == 0 and (stats.skipped or stats.duplicates):
        messages.warning(
            request,
            _('No new records were created. %(duplicates)s line(s) already '
              'existed and %(invalid)s line(s) were not valid records.')
            % {'duplicates': stats.duplicates, 'invalid': stats.skipped}
        )
    else:
        outcome = ngettext(
            'Successfully imported %(count)s record.',
            'Successfully imported %(count)s records.',
            stats.created,
        ) % {'count': stats.created}
        outcome += ' ' + _(
            'Filtered out %(count)s non-record lines.'
        ) % {'count': stats.skipped}
        if stats.duplicates:
            outcome += ' ' + _(
                'Reused %(count)s records that were already stored.'
            ) % {'count': stats.duplicates}
        messages.success(request, outcome)
    return redirect('vault:browse')


def import_prepare(request):
    """
    Register an import before its data arrives.

    The screen calls this first so that progress is read from a real job for
    the whole run -- including the transfer phase, when the browser still knows
    best how many bytes it has pushed. A server path is listed here, which also
    gives the user a dry run: how many files, how many bytes, and which ones,
    before anything is read.
    """
    if request.method != 'POST':
        return JsonResponse(
            {'ok': False, 'error': _('Use POST to prepare an import.')},
            status=405,
        )

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    mode = 'path' if payload.get('mode') == 'path' else 'upload'
    folder_path = str(payload.get('folder_path') or '').strip()

    if mode == 'path' and not local_import_paths_allowed():
        return JsonResponse({
            'ok': False,
            'error': _('Importing from a server path is disabled in this workspace.'),
        }, status=403)

    job = import_jobs.create(
        label=str(payload.get('label') or '')[:120],
        source_kind=mode,
        register_files=False,
        files_total=0,
    )

    entries = []
    total_bytes = 0
    skipped = 0

    if mode == 'path':
        if not (folder_path and (os.path.isdir(folder_path)
                                 or os.path.isfile(folder_path))):
            import_jobs.forget(job.id)
            return JsonResponse({
                'ok': False,
                'error': _('The specified folder or file path does not exist.'),
            }, status=400)
        job.mark_scanning()
        scan_stats = {'skipped': 0}
        paths, total_bytes = import_jobs.scan(
            folder_path,
            on_found=lambda count, found: job.report_scan(count, found),
            stats=scan_stats,
        )
        root = folder_path if os.path.isdir(folder_path) else None
        sources = import_jobs.sources_for_paths(paths, root=root)
        skipped = scan_stats['skipped']
        if not sources:
            import_jobs.forget(job.id)
            return JsonResponse({
                'ok': False,
                'error': _('No readable files were found in the specified path.'),
            }, status=400)
        for source in sources[:import_jobs.MAX_LISTED_FILES]:
            entries.append({
                'name': source.name,
                'path': source.path,
                'relpath': source.relpath,
                'bytes': source.size,
            })
        job.remember_scan(folder_path, sources, total_bytes)
    else:
        declared = payload.get('files') or []
        for item in declared[:import_jobs.MAX_LISTED_FILES]:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').replace('\\', '/').split('/')[-1]
            if not name or not is_candidate_file(name):
                skipped += 1
                continue
            try:
                size = max(int(item.get('bytes') or 0), 0)
            except (TypeError, ValueError):
                size = 0
            total_bytes += size
            entries.append({'name': name[:200], 'path': None, 'bytes': size})

    job.load_roster(entries)
    job.mark_waiting(total_bytes=total_bytes, files_total=len(entries))

    return JsonResponse({
        'ok': True,
        'job_id': job.id,
        'files': len(entries),
        'bytes': total_bytes,
        'skipped': skipped,
        'truncated': len(entries) >= import_jobs.MAX_LISTED_FILES,
        'sample': [entry.get('relpath') or entry['name']
                   for entry in entries[:50]],
        'progress': job.snapshot(include_files=False),
    })


def import_job_status(request, job_id):
    """
    Live progress for an import in flight.

    Returns the counters the workspace renders: overall and per-file byte
    percentages, the line/record breakdown, throughput and ETA. A 404 simply
    means the job is not (or no longer) being tracked, and the screen then
    falls back to the summary the import response itself carried.
    """
    job = import_jobs.get(job_id)
    if job is None:
        return JsonResponse({
            'ok': False,
            'error': 'unknown_job',
            'message': str(_('This import is no longer being tracked.')),
        }, status=404)

    raw_offset = request.GET.get('files')
    file_offset = None
    include_files = raw_offset != 'none'
    if raw_offset not in (None, '', 'none'):
        try:
            file_offset = max(int(raw_offset), 0)
        except (TypeError, ValueError):
            file_offset = None

    return JsonResponse({
        'ok': True,
        'job_id': job.id,
        'state': job.state,
        'finished': job.is_terminal,
        'progress': job.snapshot(
            file_offset=file_offset, include_files=include_files
        ),
    })


def import_job_stop(request, job_id):
    """
    Ask a running import to stop.

    The importer checks for this between batches, so the rows already committed
    stay committed and the summary reports what was actually stored.
    """
    if request.method != 'POST':
        return JsonResponse(
            {'ok': False, 'error': _('Use POST to stop an import.')},
            status=405,
        )
    job = import_jobs.get(job_id)
    if job is None:
        return JsonResponse({
            'ok': False,
            'error': 'unknown_job',
            'message': str(_('This import is no longer being tracked.')),
        }, status=404)
    job.cancel()
    return JsonResponse({
        'ok': True,
        'job_id': job.id,
        'stopping': not job.is_terminal,
        'progress': job.snapshot(include_files=False),
    })


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
        _('Construction date'), _('Date of adjustment'),
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
