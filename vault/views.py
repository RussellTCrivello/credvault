"""
Views for the vault.

Every function here replaces a branch of the original CLI ``main_menu()``.
The three that carry the heavy requirements are:

* :func:`search_records` -- an exact, index-backed lookup by domain, URL,
  email, username, password, or IP. It answers in milliseconds whether the
  workspace holds 10 records or 10 million, and it reports the *whole* result
  set (the page is only a presentation detail).
* :func:`export_csv` / :func:`export_json` -- stream the entire result set
  behind a search, not just what is on screen.
* :func:`import_from_file` -- ingest a file, a browser folder pick, or a
  folder already on the server, line by line, storing every line that meets
  the requirements and reusing every value that already exists.
"""

import csv
import io
import json
import os
import time

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, ProtectedError, Q
from django.db.models.functions import Lower
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.utils.translation import ngettext

from .forms import (
    AccountForm, AdvancedSearchForm, ImportFileForm, QuickSearchForm,
    ReferenceForm,
)
from .importer import ImportStats, collect_files, import_paths, import_streams
from .models import Account, Email, Ip, Pass, Url, User
from .utils import TEXT_EXTENSIONS, extract_domain, is_importable_name

# Reference-table registry used by the relationship management pages.
REFERENCE_REGISTRY = {
    'url': {'model': Url, 'label': _lazy('URL'), 'label_plural': _lazy('URLs')},
    'email': {'model': Email, 'label': _lazy('Email'),
              'label_plural': _lazy('Emails')},
    'user': {'model': User, 'label': _lazy('Username'),
             'label_plural': _lazy('Usernames')},
    'pass': {'model': Pass, 'label': _lazy('Password'),
             'label_plural': _lazy('Passwords')},
    'ip': {'model': Ip, 'label': _lazy('IP address'),
           'label_plural': _lazy('IP addresses')},
}

# Quick-search field -> (reference model, Account column, lookup column).
# Every entry resolves through an index, which is what keeps the search fast
# no matter how large the vault grows.
QUICK_SEARCH = {
    'domain': (Url, 'url_id', 'host'),
    'url': (Url, 'url_id', 'name'),
    'email': (Email, 'email_id', 'name'),
    'user': (User, 'user_id', 'name'),
    'password': (Pass, 'password_id', 'name'),
    'ip': (Ip, 'ip_id', 'name'),
}

# Reuses the labels the rest of the interface translates for these values.
QUICK_SEARCH_LABELS = {
    'domain': _lazy('Domain'),
    'url': _lazy('URL'),
    'email': _lazy('Email'),
    'user': _lazy('Username'),
    'password': _lazy('Password'),
    'ip': _lazy('IP address'),
}

PER_PAGE = 100

# Global counts are the only queries whose cost follows the size of the whole
# table, so they are cached briefly and cleared by every write.
_COUNT_CACHE = {}
_COUNT_TTL = 15


def _cached_count(key, queryset, ttl=_COUNT_TTL):
    now = time.monotonic()
    hit = _COUNT_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = queryset.count()
    _COUNT_CACHE[key] = (now, value)
    return value


def _invalidate_counts():
    _COUNT_CACHE.clear()


# ---------------------------------------------------------------- search ----

# How many reference ids are worth inlining as SQL parameters. Reference
# values are unique, so a name lookup almost always resolves to a single id;
# only a "many URLs share one host" search reaches for the subquery path.
ID_INLINE_LIMIT = 500


def _reference_id_query(model, column, needle):
    """An id subquery, used when there are too many ids to inline."""
    if column == 'host':
        return model.objects.filter(host=needle).order_by().values('id')
    return model.objects.annotate(
        match_key=Lower(column)
    ).filter(match_key=needle).order_by().values('id')


def _exact_reference_ids(model, column, value):
    """
    Ids of the reference rows whose value equals ``value``, ignoring case.

    Resolved up front because it is nearly free (a unique index lookup) and it
    lets a single hit be filtered with plain equality, which is the form the
    relationship indexes can serve *in order* -- no temporary sort, whatever
    the size of the result set.

    Returns ``(ids, truncated)``: the caller falls back to a subquery when a
    value is shared by more ids than SQL parameters should carry.
    """
    needle = (value or '').strip().lower()
    if column == 'host':
        qs = model.objects.filter(host=needle)
    else:
        qs = model.objects.annotate(
            match_key=Lower(column)
        ).filter(match_key=needle)
    ids = list(
        qs.order_by().values_list('id', flat=True)[:ID_INLINE_LIMIT + 1]
    )
    return ids[:ID_INLINE_LIMIT], len(ids) > ID_INLINE_LIMIT


def _apply_reference_filter(qs, model, column, lookup, value):
    """
    Restrict an Account queryset to the records pointing at one value.

    ``column`` is Account's own field ('url_id', 'email_id', ...), while
    ``lookup`` is the column on the reference table that is matched.
    """
    needle = (value or '').strip().lower()
    ids, truncated = _exact_reference_ids(model, lookup, value)
    if truncated:
        return qs.filter(**{
            f'{column}__in': _reference_id_query(model, lookup, needle)
        })
    if len(ids) == 1:
        # Plain equality: the relationship index then answers the filter *and*
        # the newest-first ordering, so no temporary sort is needed.
        return qs.filter(**{column: ids[0]})
    if not ids:
        return qs.none()
    return qs.filter(**{f'{column}__in': ids})


def filter_accounts(params):
    """
    Build an Account queryset from URL query parameters.

    Priority:
      1. field + q  -> exact (case-insensitive) match on one reference
         table. Indexed, so it is instant at scale.
      2. keyword    -> case-insensitive substring across every referenced
         value (flexible, slower).
      3. adv == '1' -> the per-field advanced filters.
      4. otherwise  -> nothing (don't dump the whole table uninvited).
    """
    qs = Account.objects.select_related(
        'url', 'email', 'user', 'password', 'ip'
    )

    field = params.get('field')
    q = (params.get('q') or '').strip()
    keyword = (params.get('keyword') or '').strip()

    if field and q and field in QUICK_SEARCH:
        model, column, lookup = QUICK_SEARCH[field]
        qs = _apply_reference_filter(qs, model, column, lookup, q)
    elif keyword:
        needle = keyword.strip()
        qs = qs.filter(
            Q(url__name__icontains=needle)
            | Q(url__host__icontains=needle)
            | Q(email__name__icontains=needle)
            | Q(user__name__icontains=needle)
            | Q(password__name__icontains=needle)
            | Q(ip__name__icontains=needle)
        )
    elif params.get('adv') == '1':
        qs = _apply_advanced_filters(qs, params)
    else:
        qs = qs.none()

    return qs


def _apply_advanced_filters(qs, params):
    """The slower, flexible path: every filter is optional and AND-ed."""
    domain = (params.get('domain') or '').strip()
    if domain:
        qs = qs.filter(url__host__icontains=domain)
    url = (params.get('url') or '').strip()
    if url:
        qs = qs.filter(url__name__icontains=url)
    account_type = params.get('account_type', '')
    if account_type:
        qs = qs.filter(account_type=account_type)
    email = (params.get('email') or '').strip()
    if email:
        qs = qs.filter(email__name__icontains=email)
    username = (params.get('username') or '').strip()
    if username:
        qs = qs.filter(user__name__icontains=username)
    password = (params.get('password') or '').strip()
    if password:
        qs = qs.filter(password__name__icontains=password)
    ip = (params.get('ip') or '').strip()
    if ip:
        qs = qs.filter(ip__name__icontains=ip)
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
        qs = qs.order_by('-created_at', '-id')
    return qs


def _paginate(request, queryset, per_page=PER_PAGE):
    """Paginate for display only -- exports always cover the full queryset."""
    paginator = Paginator(queryset, per_page)
    page_obj = paginator.get_page(request.GET.get('page', '1'))
    return paginator, page_obj


def _export_params(request, params):
    """Query dict without the page cursor, for links that keep the filters."""
    payload = params.copy() if hasattr(params, 'copy') else dict(params)
    payload.pop('page', None)
    return payload


def _query_string(params):
    return params.urlencode() if hasattr(params, 'urlencode') \
        else '&'.join(f'{k}={v}' for k, v in params.items())


# ------------------------------------------------------------- dashboard ----

def dashboard(request):
    """Replaces: main_menu() -- the landing page with workspace stats."""
    reference_rows = []
    for kind, registry in REFERENCE_REGISTRY.items():
        reference_rows.append({
            'kind': kind,
            'label': registry['label'],
            'label_plural': registry['label_plural'],
            'count': _cached_count(
                f'ref:{kind}', registry['model'].objects.all()
            ),
        })

    return render(request, 'vault/dashboard.html', {
        'total_records': _cached_count('account', Account.objects.all()),
        'reference_rows': reference_rows,
        'reference_registry': REFERENCE_REGISTRY,
    })


def browse_all(request):
    """
    Replaces: browse_all() -- lists the login (Account) records.

    Paginated: the page is a window onto the workspace, never a full scan, so
    the list still opens instantly with millions of records. Use the export
    buttons in the header to take everything.
    """
    records = Account.objects.select_related(
        'url', 'email', 'user', 'password', 'ip'
    ).all()
    paginator, page_obj = _paginate(request, records)
    return render(request, 'vault/browse.html', {
        'records': page_obj.object_list,
        'page_obj': page_obj,
        'paginator': paginator,
        'total': paginator.count,
        'query_string': _query_string(_export_params(request, request.GET)),
    })


def search_records(request):
    """
    Instant exact search on one field.

    The count, the page, and the export links all describe the same queryset,
    so "export" hands over every match -- not just the 100 shown here.
    """
    form = QuickSearchForm(request.GET or None)
    records = []
    page_obj = paginator = None
    total = 0
    elapsed_ms = None
    searched = False
    note = ''

    params = request.GET.copy()
    field = params.get('field')
    q = (params.get('q') or '').strip()

    if q and field in QUICK_SEARCH:
        searched = True
        started = time.perf_counter()
        queryset = filter_accounts(params)
        paginator, page_obj = _paginate(request, queryset)
        total = paginator.count
        records = page_obj.object_list
        elapsed_ms = (time.perf_counter() - started) * 1000
        note = _('Exact match on %(field)s: %(value)s') % {
            'field': QUICK_SEARCH_LABELS.get(field, field),
            'value': q,
        }
    elif (params.get('keyword') or '').strip():
        searched = True
        started = time.perf_counter()
        queryset = filter_accounts(params)
        paginator, page_obj = _paginate(request, queryset)
        total = paginator.count
        records = page_obj.object_list
        elapsed_ms = (time.perf_counter() - started) * 1000
        note = _('Contains: %(value)s') % {
            'value': params.get('keyword').strip()
        }

    export_params = _export_params(request, params)
    return render(request, 'vault/search.html', {
        'form': form,
        'records': records,
        'page_obj': page_obj,
        'paginator': paginator,
        'total': total,
        'searched': searched,
        'note': note,
        'elapsed_ms': elapsed_ms,
        'query_string': _query_string(export_params),
    })


def advanced_search(request):
    """
    Advanced per-field queries: each field is optional and combined with AND;
    an optional keyword is OR-ed across all referenced values. Results are
    paginated for display and fully exportable.
    """
    form = AdvancedSearchForm(request.GET or None)
    records = []
    page_obj = paginator = None
    total = 0
    elapsed_ms = None

    if request.GET:
        params = request.GET.copy()
        params['adv'] = '1'
        started = time.perf_counter()
        queryset = filter_accounts(params)
        paginator, page_obj = _paginate(request, queryset)
        total = paginator.count
        records = page_obj.object_list
        elapsed_ms = (time.perf_counter() - started) * 1000
        export_params = _export_params(request, params)
        query_string = _query_string(export_params)
    else:
        query_string = ''

    return render(request, 'vault/advanced_search.html', {
        'form': form,
        'records': records,
        'page_obj': page_obj,
        'paginator': paginator,
        'total': total,
        'searched': bool(request.GET),
        'elapsed_ms': elapsed_ms,
        'query_string': query_string,
    })


# ------------------------------------------------------------- CRUD ------

def view_detail(request, pk):
    """Replaces: view_detail() -- shows one login (Account) record."""
    record = get_object_or_404(
        Account.objects.select_related('url', 'email', 'user', 'password',
                                       'ip'),
        pk=pk,
    )
    return render(request, 'vault/detail.html', {'record': record})


def _reference_choices(kind, limit=500):
    """Existing names for a reference kind, used to power autocomplete."""
    model = REFERENCE_REGISTRY[kind]['model']
    return list(model.objects.values_list('name', flat=True)[:limit])


def _render_account_form(request, form, template, record=None):
    context = {
        'form': form,
        'record': record,
        'url_options': _reference_choices('url'),
        'email_options': _reference_choices('email'),
        'user_options': _reference_choices('user'),
        'ip_options': _reference_choices('ip'),
    }
    return render(request, template, context)


def add_account(request):
    """Replaces: add_record() -- creates a login with deduplicated parts."""
    if request.method == 'POST':
        form = AccountForm(request.POST)
        if form.is_valid():
            try:
                account = form.save()
            except (ValidationError, IntegrityError) as error:
                form.add_error(
                    None, getattr(error, 'messages', None) or str(error)
                )
                return _render_account_form(request, form, 'vault/add.html')
            _invalidate_counts()
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
            except (ValidationError, IntegrityError) as error:
                form.add_error(
                    None, getattr(error, 'messages', None) or str(error)
                )
                return _render_account_form(
                    request, form, 'vault/update.html', record=record
                )
            _invalidate_counts()
            messages.success(
                request,
                _('Record #%(record_id)s updated successfully.')
                % {'record_id': pk}
            )
            return redirect('vault:detail', pk=account.id)
    else:
        # instance=record pre-fills the form with current values.
        form = AccountForm(instance=record)

    return _render_account_form(
        request, form, 'vault/update.html', record=record
    )


def delete_account(request, pk):
    """Replaces: delete_record() -- POST confirms deletion of a login."""
    record = get_object_or_404(
        Account.objects.select_related('url', 'email', 'user'), pk=pk
    )
    object_name = record.identifier

    if request.method == 'POST':
        record.delete()
        _invalidate_counts()
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


# ------------------------------------------------- reference management ----

def references_index(request):
    """Overview of the five reference tables and how many values each holds."""
    reference_rows = []
    for kind, registry in REFERENCE_REGISTRY.items():
        reference_rows.append({
            'kind': kind,
            'label': registry['label'],
            'label_plural': registry['label_plural'],
            'count': _cached_count(
                f'ref:{kind}', registry['model'].objects.all()
            ),
        })
    return render(request, 'vault/references_index.html', {
        'reference_rows': reference_rows,
        'total_records': _cached_count('account', Account.objects.all()),
    })


def reference_list(request, kind):
    """List, find, and add values in one of the five reference tables."""
    if kind not in REFERENCE_REGISTRY:
        return redirect('vault:dashboard')

    registry = REFERENCE_REGISTRY[kind]
    model = registry['model']

    if request.method == 'POST':
        form = ReferenceForm(
            request.POST, kind=kind, label=str(registry['label'])
        )
        if form.is_valid():
            name = form.cleaned_data['name']
            defaults = {}
            if kind == 'url':
                defaults['host'] = extract_domain(name)
            entity, created = model.objects.get_or_create(
                name=name, defaults=defaults
            )
            _invalidate_counts()
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
        form = ReferenceForm(kind=kind, label=str(registry['label']))

    entities = model.objects.all()
    needle = (request.GET.get('q') or '').strip()
    if needle:
        entities = entities.filter(name__icontains=needle)
    paginator = Paginator(entities.order_by('name'), PER_PAGE)
    page_obj = paginator.get_page(request.GET.get('page', '1'))

    # Usage counts only for the rows on this page: counting them for every
    # value of a million-row table would be the slowest thing on the screen.
    page_ids = [entity.pk for entity in page_obj.object_list]
    usage = {}
    if page_ids:
        usage = dict(
            model.objects.filter(pk__in=page_ids)
            .annotate(usage=Count('accounts'))
            .values_list('pk', 'usage')
        )
    for entity in page_obj.object_list:
        entity.usage = usage.get(entity.pk, 0)

    return render(request, 'vault/references.html', {
        'kind': kind,
        'registry': registry,
        'reference_registry': REFERENCE_REGISTRY,
        'form': form,
        'entities': page_obj.object_list,
        'page_obj': page_obj,
        'paginator': paginator,
        'total': paginator.count,
        'needle': needle,
        'query_string': _query_string(
            _export_params(request, request.GET)
        ),
    })


def _reference_delete_context(registry, entity, kind, usage):
    return {
        'object': entity,
        'object_name': entity.name,
        'usage': usage,
        'mode': 'reference',
        'confirm_heading': _('Delete this %(label)s?') % {
            'label': registry['label']
        },
        'back_href': reverse('vault:references', args=[kind]),
    }


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
        except ProtectedError:
            _invalidate_counts()
            messages.error(
                request,
                ngettext(
                    'Cannot delete %(name)s: it is still used by %(count)s '
                    'record. Reassign or delete those records first.',
                    'Cannot delete %(name)s: it is still used by %(count)s '
                    'records. Reassign or delete those records first.',
                    usage,
                ) % {'name': entity.name, 'count': usage}
            )
            return render(request, 'vault/delete_confirm.html',
                          _reference_delete_context(registry, entity, kind,
                                                    usage))
        _invalidate_counts()
        messages.success(
            request,
            _('%(label)s deleted successfully.') % {'label': registry['label']}
        )
        return redirect('vault:references', kind=kind)

    usage = entity.accounts.count()
    return render(request, 'vault/delete_confirm.html',
                  _reference_delete_context(registry, entity, kind, usage))


# ---------------------------------------------------------------- import ----

def _server_import_roots():
    return [
        os.path.realpath(root)
        for root in getattr(settings, 'IMPORT_ROOTS', []) or []
    ]


def _validate_server_path(path):
    """
    Guard the server-side folder import.

    When ``IMPORT_ROOTS`` is configured a path may only be used if it lives
    inside one of those directories; otherwise any readable path is accepted
    (this workspace is a local tool, and ``manage.py importfolder`` can do the
    same thing from a shell anyway).
    """
    resolved = os.path.realpath(path)
    roots = _server_import_roots()
    if roots and not any(
        resolved == root or resolved.startswith(root + os.sep)
        for root in roots
    ):
        return None
    if not (os.path.isdir(resolved) or os.path.isfile(resolved)):
        return None
    return resolved


def import_from_file(request):
    """
    Replaces: import_from_file() -- uploads one file, many files, or a whole
    folder and stores every valid line, deduplicating referenced values in
    bulk.

    Files are streamed in batches, so a folder of very large files never has
    to fit in memory. For the largest volumes ``manage.py importfolder`` is
    still the recommended path -- it reads from disk directly.
    """
    allowed_ext = set(getattr(settings, 'IMPORT_EXTENSIONS', ()) or ())

    if request.method == 'POST':
        uploads = (
            request.FILES.getlist('file') + request.FILES.getlist('folder')
        )
        form = ImportFileForm(request.POST, request.FILES, uploads=uploads)
        if form.is_valid():
            streams = []
            skipped_files = 0
            for handle in uploads:
                name = getattr(handle, 'name', '') or ''
                if allowed_ext and not is_importable_name(name, allowed_ext):
                    skipped_files += 1
                    continue
                streams.append((name, handle))

            server_root = None
            path_error = None
            server_path = (form.cleaned_data.get('server_path') or '').strip()
            if server_path:
                server_root = _validate_server_path(server_path)
                if server_root is None:
                    path_error = _(
                        'That server path cannot be read. Check the folder '
                        'name, or add it to IMPORT_ROOTS.'
                    )

            if not streams and server_root is None:
                if path_error:
                    form.add_error('server_path', path_error)
                elif skipped_files:
                    form.add_error(
                        None,
                        _('No importable text files were found in that '
                          'selection.')
                    )
                else:
                    form.add_error(
                        None,
                        _('Please choose a file, a folder, or a server path.')
                    )
                return render(request, 'vault/import.html', {'form': form})

            started = time.perf_counter()
            stats = ImportStats(0, 0)
            if streams:
                stats = stats.merge(import_streams(streams))
            if server_root:
                paths = collect_files([server_root])
                stats = stats.merge(
                    import_paths(paths, extensions=allowed_ext
                                 or TEXT_EXTENSIONS)
                )
            elapsed = time.perf_counter() - started

            _invalidate_counts()

            if stats.created == 0:
                messages.warning(
                    request,
                    _('No new records were created. Existing values were '
                      'reused and %(count)d line(s) were not valid records.')
                    % {'count': stats.skipped}
                )
            else:
                messages.success(
                    request,
                    ngettext(
                        'Successfully imported %(count)s record.',
                        'Successfully imported %(count)s records.',
                        stats.created,
                    ) % {'count': stats.created}
                    + ' '
                    + _('Filtered out %(count)s non-record lines.')
                    % {'count': stats.skipped}
                    + ' '
                    + _('%(files)s file(s) read in %(seconds)s s.') % {
                        'files': stats.files, 'seconds': round(elapsed, 2)
                    }
                )
            return redirect('vault:browse')
    else:
        form = ImportFileForm()

    return render(request, 'vault/import.html', {'form': form})


# ---------------------------------------------------------------- export ----

#: The columns an export writes, pulled straight from the joins instead of
#: through model instances: materializing a million ``Account`` objects to
#: print them is what makes a big export slow, not the SQL.
EXPORT_FIELDS = (
    'id', 'url__host', 'url__name', 'account_type', 'email__name',
    'user__name', 'password__name', 'ip__name', 'created_at', 'updated_at',
)

CSV_HEADER = [
    _lazy('ID'), _lazy('Domain'), _lazy('URL'), _lazy('Account type'),
    _lazy('Identifier'), _lazy('Password'), _lazy('IP address'),
    _lazy('Construction date'), _lazy('Date of adjustment'),
]

JSON_KEYS = ('id', 'domain', 'url', 'account_type', 'identifier', 'password',
             'ip_address', 'created_at', 'updated_at')

ACCOUNT_TYPE_LABELS = {
    'email': _lazy('Email'),
    'user': _lazy('Username'),
}


def _export_rows(request):
    """
    The rows an export must cover.

    Search and advanced-search filters in the URL are honoured, so exporting
    from a result page gives *every* matching record rather than the page on
    screen. With no filters it covers the whole workspace. The result is a
    lazily fetched generator, so memory stays flat at any size.
    """
    params = request.GET
    has_filter = bool(params.get('field') and params.get('q')) \
        or bool((params.get('keyword') or '').strip()) \
        or params.get('adv') == '1'
    qs = filter_accounts(params) if has_filter else Account.objects.all()
    return qs.order_by('id').values_list(*EXPORT_FIELDS).iterator(
        chunk_size=2000
    )


def _export_values(row):
    """One exported record, as CSV cells and as JSON values."""
    (pk, host, url, account_type, email, user, password, ip,
     created_at, updated_at) = row
    identifier = (email if account_type == 'email' else user) or '—'
    return [
        pk,
        host,
        url,
        str(ACCOUNT_TYPE_LABELS.get(account_type, account_type)),
        identifier,
        password,
        ip or 'N/A',
        created_at.strftime('%Y-%m-%d %H:%M:%S'),
        updated_at.strftime('%Y-%m-%d %H:%M:%S'),
    ], (
        pk, host, url, account_type, identifier, password, ip or 'N/A',
        created_at.isoformat(), updated_at.isoformat(),
    )


def _account_csv_stream(rows):
    """
    Yield the CSV one row at a time.

    A UTF-8 BOM goes out first so a spreadsheet opens Arabic values correctly.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    yield '\ufeff' + buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        writer.writerow(_export_values(row)[0])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


def _account_json_stream(rows):
    yield '['
    first = True
    for row in rows:
        values = dict(zip(JSON_KEYS, _export_values(row)[1]))
        yield ('', ',')[not first] + json.dumps(values, ensure_ascii=False)
        first = False
    yield ']'


def _attachment(response, filename):
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    # Let a reverse proxy stream the body instead of buffering it.
    response['X-Accel-Buffering'] = 'no'
    return response


def export_csv(request):
    """Streams the (optionally filtered) records as CSV -- safe at scale."""
    response = StreamingHttpResponse(
        _account_csv_stream(_export_rows(request)),
        content_type='text/csv; charset=utf-8',
    )
    return _attachment(response, 'vault_export.csv')


def export_json(request):
    """Streams the (optionally filtered) records as JSON -- safe at scale."""
    response = StreamingHttpResponse(
        _account_json_stream(_export_rows(request)),
        content_type='application/json',
    )
    return _attachment(response, 'vault_export.json')
