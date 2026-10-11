import csv
import io
import json
import os
import time
from functools import cached_property

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import ProtectedError
from django.db.models.functions import Lower
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy, ngettext

from .db import (
    capped_count, index_names, is_postgresql, table_count, text_collation_is_c,
)
from .forms import (
    AccountForm, AdvancedSearchForm, PerformanceSettingsForm,
    QuickSearchForm, ReferenceForm, ImportFileForm,
)
from .importer import (
    collect_files_from_path, count_lines, import_path, import_sources,
    path_source, upload_source,
)
from .models import Account, Email, Ip, Pass, PerformanceSettings, Url, User
from .performance import IMPORT_PROFILES, QUERY_PROFILES, import_profile, query_profile
from .search import filter_accounts, has_search_query, prefix_clause

#: Largest number of usages counted per reference value (shown as "N+").
USAGE_CAP = 10_000


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


class CappedPaginator(Paginator):
    """
    Paginator for search results. The total is counted only up to ``cap``
    matches (see ``vault.db.capped_count``), so a broad search over billions of
    rows costs the same as a narrow one. ``capped`` tells the template to show
    "more than N" instead of an exact total.
    """

    def __init__(self, object_list, per_page, cap):
        super().__init__(object_list, per_page)
        self.cap = cap
        self.capped = False

    @cached_property
    def count(self):
        total, capped = capped_count(self.object_list, self.cap)
        self.capped = capped
        return total


class KnownCountPaginator(Paginator):
    """Paginator whose total is supplied by the caller (for whole-table pages)."""

    def __init__(self, object_list, per_page, total):
        super().__init__(object_list, per_page)
        self._total = total

    @cached_property
    def count(self):
        return self._total


def _clean_query_string(request_get):
    """Return URL query string without the pagination 'page' parameter."""
    params = request_get.copy()
    params.pop('page', None)
    return params.urlencode()


def _run_query(request, queryset, profile):
    """
    Page through a search queryset and return the template context for it,
    with the time the query took.
    """
    started = time.perf_counter()
    paginator = CappedPaginator(queryset, profile.page_size, profile.count_cap)
    page_obj = paginator.get_page(request.GET.get('page', '1'))
    records = list(page_obj.object_list)
    total = paginator.count
    return {
        'page_obj': page_obj,
        'records': records,
        'total': total,
        'total_capped': paginator.capped,
        'count_cap': profile.count_cap,
        'query_ms': round((time.perf_counter() - started) * 1000, 1),
        'query_profile': profile,
    }


def _reference_choices(kind):
    """Existing names for a reference kind, used to power autocomplete."""
    model = REFERENCE_REGISTRY[kind]['model']
    return list(model.objects.values_list('name', flat=True)[:200])


def _reference_overview():
    """Per-table value counts (approximate for large PostgreSQL tables)."""
    rows = []
    for kind in REFERENCE_REGISTRY:
        count, approximate = table_count(REFERENCE_REGISTRY[kind]['model'])
        rows.append({
            'kind': kind,
            'label': REFERENCE_REGISTRY[kind]['label'],
            'label_plural': REFERENCE_REGISTRY[kind]['label_plural'],
            'count': count,
            'approximate': approximate,
        })
    return rows


def dashboard(request):
    """Replaces: main_menu() -- the landing page with workspace stats."""
    total_records, total_approximate = table_count(Account)
    return render(request, 'vault/dashboard.html', {
        'total_records': total_records,
        'total_records_approximate': total_approximate,
        'reference_rows': _reference_overview(),
        'reference_registry': REFERENCE_REGISTRY,
        'quick_form': QuickSearchForm(),
        'query_profile': query_profile(),
        'import_profile': import_profile(),
        'engine_name': connection.display_name,
    })


def browse_all(request):
    """
    Records view: never dumps unqueried records across millions of rows.
    Displays matching records only when the user submits a search query.
    """
    form = QuickSearchForm(request.GET or None)
    has_query = has_search_query(request.GET)
    context = {
        'form': form,
        'has_query': has_query,
        'records': [],
        'page_obj': None,
        'total': 0,
        'total_capped': False,
        'query_string': _clean_query_string(request.GET),
    }
    if has_query:
        profile = query_profile()
        context.update(_run_query(request, filter_accounts(request.GET, profile), profile))
    total_records, total_approximate = table_count(Account)
    context['total_records'] = total_records
    context['total_records_approximate'] = total_approximate
    return render(request, 'vault/browse.html', context)


def search_records(request):
    """
    Fast, indexed search across domain, URL, email, username, password, or IP.
    Results are only queried and displayed when the user submits a query, and
    the full matching dataset can be exported in CSV or JSON.
    """
    form = QuickSearchForm(request.GET or None)
    has_query = has_search_query(request.GET)
    context = {
        'form': form,
        'has_query': has_query,
        'records': [],
        'page_obj': None,
        'total': 0,
        'total_capped': False,
        'query_string': _clean_query_string(request.GET),
    }
    if has_query:
        profile = query_profile()
        context.update(_run_query(request, filter_accounts(request.GET, profile), profile))
    return render(request, 'vault/search.html', context)


def advanced_search(request):
    """
    Advanced per-field queries: each field is optional and combined with
    AND; an optional keyword is OR-ed across all referenced values.
    Results are paginated and fully exportable.
    """
    submitted = bool(request.GET)
    form = AdvancedSearchForm(request.GET if submitted else None)
    context = {
        'form': form,
        'records': [],
        'page_obj': None,
        'total': 0,
        'total_capped': False,
        'query_string': _clean_query_string(request.GET),
    }
    if submitted and form.is_valid():
        params = request.GET.copy()
        params['adv'] = '1'
        profile = query_profile()
        context.update(_run_query(request, filter_accounts(params, profile), profile))
    return render(request, 'vault/advanced_search.html', context)


def _identifier_for(account_type, email, user):
    return email if account_type == 'email' else user


def search_api(request):
    """
    JSON search endpoint for scripts and the search page.

    Returns at most the profile's ``api_limit`` rows (no passwords) and a
    count capped at the profile's ``count_cap``.
    """
    profile = query_profile()
    if not has_search_query(request.GET):
        return JsonResponse({
            'results': [], 'total': 0, 'total_capped': False,
            'query_ms': 0.0, 'query_mode': profile.key,
        })
    started = time.perf_counter()
    queryset = filter_accounts(request.GET, profile)
    total, capped = capped_count(queryset, profile.count_cap)
    rows = queryset.values_list(
        'id', 'account_type', 'url__name', 'email__name', 'user__name',
        'ip__name', 'created_at',
    )[:profile.api_limit]
    results = [
        {
            'id': pk,
            'url': url,
            'account_type': account_type,
            'identifier': _identifier_for(account_type, email, user),
            'ip_address': ip,
            'created_at': created.isoformat(),
        }
        for pk, account_type, url, email, user, ip, created in rows
    ]
    return JsonResponse({
        'results': results,
        'total': total,
        'total_capped': capped,
        'query_ms': round((time.perf_counter() - started) * 1000, 1),
        'query_mode': profile.key,
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


def references_index(request):
    """Overview of the five reference tables and how many values each holds."""
    total_records, total_approximate = table_count(Account)
    return render(request, 'vault/references_index.html', {
        'reference_rows': _reference_overview(),
        'total_records': total_records,
        'total_records_approximate': total_approximate,
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

    total_values, total_approximate = table_count(model)
    qs = model.objects.order_by('name')
    # The total is known from table_count, so paging never counts the table.
    paginator = KnownCountPaginator(qs, 100, total_values)
    page_obj = paginator.get_page(request.GET.get('page', '1'))
    entities = list(page_obj.object_list)
    for entity in entities:
        usage, capped = capped_count(entity.accounts.all(), USAGE_CAP)
        entity.usage_text = f'{usage}+' if capped else str(usage)

    return render(request, 'vault/references.html', {
        'kind': kind,
        'registry': registry,
        'reference_registry': REFERENCE_REGISTRY,
        'form': form,
        'entities': entities,
        'page_obj': page_obj,
        'total_values': total_values,
        'total_values_approximate': total_approximate,
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
        usage, capped = capped_count(entity.accounts.all(), USAGE_CAP)
        usage_label = f'{usage}+' if capped else usage
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
                ) % {'name': entity.name, 'count': usage_label}
            )
            return render(request, 'vault/delete_confirm.html', {
                'object': entity,
                'object_name': entity.name,
                'usage': usage_label,
                'mode': 'reference',
                'confirm_heading': _('Delete this %(label)s?')
                % {'label': registry['label']},
                'back_href': reverse('vault:references', args=[kind]),
            })

    usage, capped = capped_count(entity.accounts.all(), USAGE_CAP)
    return render(request, 'vault/delete_confirm.html', {
        'object': entity,
        'object_name': entity.name,
        'usage': f'{usage}+' if capped else usage,
        'mode': 'reference',
        'confirm_heading': _('Delete this %(label)s?')
        % {'label': registry['label']},
        'back_href': reverse('vault:references', args=[kind]),
    })


def reference_suggest(request, kind):
    """
    Autocomplete for the reference fields: names that start with the typed
    text first, then names that contain it.
    """
    if kind not in REFERENCE_REGISTRY:
        return JsonResponse({'results': []})
    model = REFERENCE_REGISTRY[kind]['model']
    q = (request.GET.get('q') or '').strip()
    limit = 50

    if not q:
        results = list(model.objects.values_list('name', flat=True)[:limit])
        return JsonResponse({'results': results})

    prefix_matches = list(
        model.objects.annotate(_n=Lower('name'))
        .filter(prefix_clause(q.lower()))
        .values_list('name', flat=True)[:limit]
    )
    if len(prefix_matches) < limit:
        contains_matches = list(
            model.objects.filter(name__contains_ci=q)
            .exclude(name__in=prefix_matches)
            .values_list('name', flat=True)[:limit - len(prefix_matches)]
        )
        prefix_matches.extend(contains_matches)
    return JsonResponse({'results': prefix_matches})


def _import_error(message):
    return json.dumps({'type': 'error', 'message': message}) + '\n'


def _progress_event(event, total_lines, total_bytes, files_completed):
    """One NDJSON progress line from a batch or file event of import_sources."""
    if total_lines:
        percent = event['lines_done'] / total_lines * 100
    elif total_bytes:
        percent = event['bytes_done'] / total_bytes * 100
    else:
        percent = 0.0
    return json.dumps({
        'type': 'progress',
        'phase': 'processing',
        'processed': event['lines_done'],
        'total': total_lines,
        'percent': round(min(percent, 100.0), 2),
        'file_number': event['file_index'],
        'total_files': event['total_files'],
        'files_completed': files_completed,
        'created': event['created'],
        'skipped': event['skipped'],
        'rate': round(event['rate'], 1),
        'elapsed': round(event['elapsed'], 2),
    }) + '\n'


def _stream_import_progress(uploaded_files, folder_path, profile):
    """
    Import inputs while streaming progress to the browser as NDJSON.

    Balanced counts lines first, so the progress bar is exact by lines. Fast and
    Maximum skip that pass: the progress is measured by bytes read, which needs
    no extra read of the file.
    """
    def events():
        if profile.count_lines:
            yield json.dumps({'type': 'phase', 'phase': 'counting'}) + '\n'

        try:
            sources = []
            if folder_path:
                sources.extend(
                    path_source(filepath)
                    for filepath in collect_files_from_path(folder_path, ext='*')
                )
            sources.extend(upload_source(f) for f in uploaded_files)
        except OSError:
            yield _import_error(_('A selected file could not be read.'))
            return

        if not sources:
            yield _import_error(
                _('No readable files were found in the specified path.')
            )
            return

        total_files = len(sources)
        if profile.count_lines:
            counted = []
            for file_number, source in enumerate(sources, start=1):
                try:
                    line_count = count_lines(source)
                except OSError:
                    yield _import_error(_('A selected file could not be read.'))
                    return
                counted.append(source._replace(line_count=line_count))
                yield json.dumps({
                    'type': 'count_progress',
                    'files_scanned': file_number,
                    'total_files': total_files,
                }) + '\n'
            sources = counted

        total_lines = (
            sum(source.line_count for source in sources)
            if profile.count_lines else 0
        )
        total_bytes = sum(source.size for source in sources)

        yield json.dumps({
            'type': 'progress',
            'phase': 'processing',
            'processed': 0,
            'total': total_lines,
            'percent': 0.0,
            'mode': profile.key,
        }) + '\n'

        for event in import_sources(sources, profile):
            kind = event['type']
            if kind == 'batch':
                yield _progress_event(
                    event, total_lines, total_bytes,
                    files_completed=event['file_index'] - 1,
                )
            elif kind == 'file':
                yield _progress_event(
                    event, total_lines, total_bytes,
                    files_completed=event['index'],
                )
            elif kind == 'error':
                message = _(
                    'A selected file changed while it was being imported. '
                    'The progress total is no longer accurate.'
                ) if event['reason'] == 'changed' else _(
                    'A file changed or became unreadable during import.'
                )
                yield _import_error(message)
                break
            elif kind == 'done':
                yield json.dumps({
                    'type': 'done',
                    'created': event['created'],
                    'skipped': event['skipped'],
                    'files': event['files'],
                    'lines': event['lines'],
                    'elapsed': round(event['elapsed'], 2),
                    'rate': round(event['rate'], 1),
                    'mode': profile.key,
                    'mode_label': str(profile.label),
                    'redirect': reverse('vault:browse'),
                }) + '\n'

    response = StreamingHttpResponse(
        events(), content_type='application/x-ndjson; charset=utf-8'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def _consume_import(sources, profile):
    """Run an import to the end and return its final totals (no progress)."""
    totals = {'created': 0, 'skipped': 0}
    for event in import_sources(sources, profile):
        if event['type'] == 'done':
            totals = event
    return totals


def import_from_file(request):
    """
    Replaces: import_from_file() -- uploads one file, a whole folder of files,
    or reads a local/server directory or file path directly from disk, storing
    every line that meets the record format requirements and deduplicating
    referenced values in bulk.
    """
    if request.method == 'POST':
        progress_request = request.headers.get('X-Import-Progress') == '1'
        form = ImportFileForm(request.POST, request.FILES)
        if form.is_valid():
            files = []
            files.extend(request.FILES.getlist('file'))
            files.extend(request.FILES.getlist('folder'))
            folder_path = (form.cleaned_data.get('folder_path') or '').strip()

            if not files and not folder_path:
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

            profile = import_profile()

            if progress_request:
                if folder_path and not (
                    os.path.isdir(folder_path) or os.path.isfile(folder_path)
                ):
                    return JsonResponse(
                        {'error': _('The specified folder or file path does not exist.')},
                        status=400,
                    )
                return _stream_import_progress(files, folder_path, profile)

            created_total = 0
            skipped_total = 0

            if folder_path:
                if not (os.path.isdir(folder_path) or os.path.isfile(folder_path)):
                    form.add_error(
                        'folder_path',
                        _('The specified folder or file path does not exist.')
                    )
                    return render(request, 'vault/import.html', {'form': form})
                created_path, skipped_path, matched_files = import_path(
                    folder_path, ext="*", profile=profile,
                )
                if matched_files == 0 and not files:
                    form.add_error(
                        'folder_path',
                        _('No readable files were found in the specified path.')
                    )
                    return render(request, 'vault/import.html', {'form': form})
                created_total += created_path
                skipped_total += skipped_path

            if files:
                totals = _consume_import(
                    [upload_source(uploaded) for uploaded in files], profile,
                )
                created_total += totals['created']
                skipped_total += totals['skipped']

            if created_total == 0 and skipped_total > 0:
                messages.warning(
                    request,
                    _('No new records were created. Existing values were '
                      'reused and %d line(s) were not valid records.')
                    % skipped_total
                )
            else:
                messages.success(
                    request,
                    ngettext(
                        'Successfully imported %(count)s record.',
                        'Successfully imported %(count)s records.',
                        created_total,
                    )
                    % {'count': created_total}
                    + ' '
                    + _('Filtered out %(count)s non-record lines.')
                    % {'count': skipped_total}
                )
            return redirect('vault:browse')
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

    return render(request, 'vault/import.html', {
        'form': form,
        'import_profile': import_profile(),
        'import_profiles': IMPORT_PROFILES.values(),
        'engine_name': connection.display_name,
    })


def _search_index_status():
    """For each reference table: whether its prefix and trigram indexes exist."""
    names = index_names()
    rows = []
    for kind, registry in REFERENCE_REGISTRY.items():
        table = registry['model']._meta.db_table
        prefix_present = f'{table}_name_prefix_idx' in names
        # Under the C collation the lower-name index already serves prefix search.
        prefix_not_needed = (
            not prefix_present and text_collation_is_c(table, 'name')
        )
        rows.append({
            'kind': kind,
            'label_plural': registry['label_plural'],
            'prefix': prefix_present,
            'prefix_not_needed': prefix_not_needed,
            'trigram': f'{table}_name_trgm_idx' in names,
        })
    return rows


def settings_view(request):
    """The Settings page: import speed and query speed (saved once, used everywhere)."""
    stored = PerformanceSettings.load()
    if request.method == 'POST':
        form = PerformanceSettingsForm(request.POST, instance=stored)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                _('Settings saved. They apply to the next import or search.')
            )
            return redirect('vault:settings')
    else:
        form = PerformanceSettingsForm(instance=stored)

    postgres = is_postgresql()
    pg_version = None
    if postgres:
        version = connection.pg_version  # e.g. 160014 for 16.14
        pg_version = f'{version // 10000}.{version % 10000}'
    total_records, total_approximate = table_count(Account)
    return render(request, 'vault/settings.html', {
        'form': form,
        'import_profiles': IMPORT_PROFILES.values(),
        'query_profiles': QUERY_PROFILES.values(),
        'active_import': IMPORT_PROFILES[stored.import_mode],
        'active_query': QUERY_PROFILES[stored.query_mode],
        'engine_name': connection.display_name,
        'is_postgresql': postgres,
        'pg_version': pg_version,
        'index_status': _search_index_status() if postgres else [],
        'total_records': total_records,
        'total_records_approximate': total_approximate,
    })


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
