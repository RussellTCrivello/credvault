import csv
import io
import json
import os
from urllib.parse import urlparse

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
from .importer import (
    STREAM_BATCH_SIZE, bulk_import_lines, collect_files_from_path, import_path,
)
from .models import Account, Email, Ip, Pass, Url, User
from .utils import extract_domain, parse_line


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

    ids = (
        Url.objects.annotate(_n=Lower('name'))
        .filter(clauses)
        .values('id')
    )
    if not ids.exists() and force_domain:
        dom = extract_domain(q_lower)
        if dom:
            ids = (
                Url.objects.filter(name__icontains=dom)
                .values('id')
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
            url_ids = (
                Url.objects.filter(name__icontains=keyword)
                .values('id')
            )
            email_ids = (
                Email.objects.filter(name__icontains=keyword)
                .values('id')
            )
            user_ids = (
                User.objects.filter(name__icontains=keyword)
                .values('id')
            )
            pass_ids = (
                Pass.objects.filter(name__icontains=keyword)
                .values('id')
            )
            ip_ids = (
                Ip.objects.filter(name__icontains=keyword)
                .values('id')
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
            ids = (
                Url.objects.filter(name__icontains=url)
                .values('id')
            )
            qs = qs.filter(url_id__in=ids)

        account_type = (params.get('account_type') or '').strip()
        if account_type:
            qs = qs.filter(account_type=account_type)

        email = (params.get('email') or '').strip()
        if email:
            ids = (
                Email.objects.filter(name__icontains=email)
                .values('id')
            )
            qs = qs.filter(email_id__in=ids)

        username = (params.get('username') or '').strip()
        if username:
            ids = (
                User.objects.filter(name__icontains=username)
                .values('id')
            )
            qs = qs.filter(user_id__in=ids)

        password = (params.get('password') or '').strip()
        if password:
            ids = (
                Pass.objects.filter(name__icontains=password)
                .values('id')
            )
            qs = qs.filter(password_id__in=ids)

        ip = (params.get('ip') or '').strip()
        if ip:
            ids = (
                Ip.objects.filter(name__icontains=ip)
                .values('id')
            )
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
        return qs.filter(url_id__in=ids)

    if field == 'url':
        ids = _match_url_ids(search_term, force_domain=False)
        return qs.filter(url_id__in=ids)

    if field in _QUICK_SEARCH_MAP:
        model, fk = _QUICK_SEARCH_MAP[field]
        ids = _match_exact_ids(model, search_term)
        return qs.filter(**{f"{fk}__in": ids})

    # Default ('all' or unspecified field or 'keyword'): check all 5 indexed
    # reference tables (Domain/URL, Email, Username, Password, IP) in < 1 ms.
    url_ids = _match_url_ids(search_term, force_domain=False)
    email_ids = _match_exact_ids(Email, search_term)
    user_ids = _match_exact_ids(User, search_term)
    pass_ids = _match_exact_ids(Pass, search_term)
    ip_ids = _match_exact_ids(Ip, search_term)

    clauses = Q()
    clauses |= Q(url_id__in=url_ids)
    clauses |= Q(email_id__in=email_ids)
    clauses |= Q(user_id__in=user_ids)
    clauses |= Q(password_id__in=pass_ids)
    clauses |= Q(ip_id__in=ip_ids)

    if any(ids.exists() for ids in (
        url_ids, email_ids, user_ids, pass_ids, ip_ids,
    )):
        return qs.filter(clauses)

    # If invoked via free-text keyword and no exact/domain match was found,
    # fall back to reference-table substring lookup.
    if keyword and not q:
        url_ids = (
            Url.objects.filter(name__icontains=keyword)
            .values('id')
        )
        email_ids = (
            Email.objects.filter(name__icontains=keyword)
            .values('id')
        )
        user_ids = (
            User.objects.filter(name__icontains=keyword)
            .values('id')
        )
        pass_ids = (
            Pass.objects.filter(name__icontains=keyword)
            .values('id')
        )
        ip_ids = (
            Ip.objects.filter(name__icontains=keyword)
            .values('id')
        )
        return qs.filter(
            Q(url_id__in=url_ids)
            | Q(email_id__in=email_ids)
            | Q(user_id__in=user_ids)
            | Q(password_id__in=pass_ids)
            | Q(ip_id__in=ip_ids)
        )

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
        total = queryset.count()
        page_obj = _paginate(request, queryset)
        records = page_obj.object_list

    return render(request, 'vault/browse.html', {
        'form': form,
        'has_query': has_query,
        'records': records,
        'page_obj': page_obj,
        'total': total,
        'total_records': Account.objects.count(),
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
        total = queryset.count()
        page_obj = _paginate(request, queryset)
        records = page_obj.object_list

    return render(request, 'vault/search.html', {
        'form': form,
        'has_query': has_query,
        'records': records,
        'page_obj': page_obj,
        'total': total,
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
        total = queryset.count()
        page_obj = _paginate(request, queryset)
        records = page_obj.object_list

    return render(request, 'vault/advanced_search.html', {
        'form': form,
        'records': records,
        'page_obj': page_obj,
        'total': total,
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


def reference_list(request, kind):
    """List and add values in one of the five reference tables."""
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

    total_values = model.objects.count()
    qs = model.objects.annotate(usage=Count('accounts')).order_by('name')
    page_obj = _paginate(request, qs, per_page=100)

    return render(request, 'vault/references.html', {
        'kind': kind,
        'registry': registry,
        'reference_registry': REFERENCE_REGISTRY,
        'form': form,
        'entities': page_obj.object_list,
        'page_obj': page_obj,
        'total_values': total_values,
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

            if progress_request:
                if folder_path and not (
                    os.path.isdir(folder_path) or os.path.isfile(folder_path)
                ):
                    return JsonResponse(
                        {'error': _('The specified folder or file path does not exist.')},
                        status=400,
                    )
                return _stream_import_progress(files, folder_path)

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
                    folder_path, ext="*"
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
                parsed_stream = (
                    parse_line(line)
                    for uploaded_file in files
                    for line in uploaded_file
                )
                created_files, skipped_files = bulk_import_lines(parsed_stream)
                created_total += created_files
                skipped_total += skipped_files

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

    return render(request, 'vault/import.html', {'form': form})


def _stream_import_progress(uploaded_files, folder_path):
    """Import inputs while streaming exact line-count progress to the browser."""
    def events():
        yield json.dumps({'type': 'phase', 'phase': 'counting'}) + '\n'

        sources = []
        if folder_path:
            sources.extend(
                ('path', filepath)
                for filepath in collect_files_from_path(folder_path, ext='*')
            )
        sources.extend(('upload', uploaded_file) for uploaded_file in uploaded_files)

        counted_sources = []
        total_lines = 0
        total_files = len(sources)
        for file_number, (source_type, source) in enumerate(sources, start=1):
            try:
                if source_type == 'upload':
                    source.seek(0)
                    line_count = sum(1 for _line in source)
                    source.seek(0)
                else:
                    with open(
                        source, 'r', encoding='utf-8', errors='ignore'
                    ) as handle:
                        line_count = sum(1 for _line in handle)
            except OSError:
                yield json.dumps({
                    'type': 'error',
                    'message': _('A selected file could not be read.'),
                }) + '\n'
                return
            counted_sources.append((source_type, source, line_count))
            total_lines += line_count
            yield json.dumps({
                'type': 'count_progress',
                'files_scanned': file_number,
                'total_files': total_files,
            }) + '\n'

        if not counted_sources and not uploaded_files:
            yield json.dumps({
                'type': 'error',
                'message': _('No readable files were found in the specified path.'),
            }) + '\n'
            return

        yield json.dumps({
            'type': 'progress',
            'phase': 'processing',
            'processed': 0,
            'total': total_lines,
        }) + '\n'

        processed_lines = 0
        created_total = 0
        skipped_total = 0
        batch_size = STREAM_BATCH_SIZE

        files_completed = 0
        for file_number, (source_type, source, line_count) in enumerate(
            counted_sources, start=1
        ):
            if source_type == 'upload':
                handle = source
                close_handle = False
            else:
                try:
                    handle = open(
                        source, 'r', encoding='utf-8', errors='ignore'
                    )
                except OSError:
                    yield json.dumps({
                        'type': 'error',
                        'message': _(
                            'A file changed or became unreadable during import.'
                        ),
                    }) + '\n'
                    return
                close_handle = True

            try:
                batch = []
                file_processed_lines = 0
                for line in handle:
                    batch.append(line)
                    file_processed_lines += 1
                    if len(batch) >= batch_size:
                        created, skipped = bulk_import_lines(
                            parse_line(item) for item in batch
                        )
                        created_total += created
                        skipped_total += skipped
                        processed_lines += len(batch)
                        batch.clear()
                        yield json.dumps({
                            'type': 'progress',
                            'phase': 'processing',
                            'processed': processed_lines,
                            'total': total_lines,
                            'file_number': file_number,
                            'total_files': len(counted_sources),
                            'files_completed': files_completed,
                        }) + '\n'
                if batch:
                    created, skipped = bulk_import_lines(
                        parse_line(item) for item in batch
                    )
                    created_total += created
                    skipped_total += skipped
                    processed_lines += len(batch)
                    yield json.dumps({
                        'type': 'progress',
                        'phase': 'processing',
                        'processed': processed_lines,
                        'total': total_lines,
                        'file_number': file_number,
                        'total_files': len(counted_sources),
                        'files_completed': files_completed,
                    }) + '\n'
            finally:
                if close_handle:
                    handle.close()
            if file_processed_lines != line_count:
                yield json.dumps({
                    'type': 'error',
                    'message': _(
                        'A selected file changed while it was being imported. '
                        'The progress total is no longer accurate.'
                    ),
                }) + '\n'
                return
            files_completed += 1
            yield json.dumps({
                'type': 'progress',
                'phase': 'processing',
                'processed': processed_lines,
                'total': total_lines,
                'file_number': file_number,
                'total_files': len(counted_sources),
                'files_completed': files_completed,
            }) + '\n'

        yield json.dumps({
            'type': 'done',
            'created': created_total,
            'skipped': skipped_total,
            'files': len(counted_sources),
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
