
import csv
import json

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.db.models import Q
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .models import Record
from .forms import RecordForm, ImportFileForm, SearchForm
from .utils import parse_line


def dashboard(request):
    """
    Replaces: main_menu()
    The landing page with navigation to all features.
    """
    total_records = Record.objects.count()
    return render(request, 'vault/dashboard.html', {
        'total_records': total_records
    })


def browse_all(request):
    """
    Replaces: browse_all() which did:
        cursor.execute("SELECT * FROM records ORDER BY id ASC")
        rows = cursor.fetchall()
        print_table(rows)
    
    Django ORM equivalent: Record.objects.all()
    The template replaces print_table().
    """
    records = Record.objects.all()
    return render(request, 'vault/browse.html', {
        'records': records
    })


def search_records(request):
    """
    Replaces: search_records() which used raw SQL with LIKE queries.
    
    Original SQL:
        SELECT * FROM records 
        WHERE LOWER(domain) LIKE ? 
           OR LOWER(url) LIKE ? 
           OR LOWER(account_identifier) LIKE ?
           ...
    
    Django ORM equivalent uses Q objects for OR conditions
    and __icontains for case-insensitive LIKE.
    """
    form = SearchForm(request.GET or None)
    records = []

    if form.is_valid():
        keyword = form.cleaned_data['keyword']
        records = Record.objects.filter(
            Q(domain__icontains=keyword) |
            Q(url__icontains=keyword) |
            Q(account_identifier__icontains=keyword) |
            Q(secret_data__icontains=keyword) |
            Q(ip_address__icontains=keyword)
        )

    return render(request, 'vault/search.html', {
        'form': form,
        'records': records
    })


def view_detail(request, record_id):
    """
    Replaces: view_detail() which did:
        cursor.execute("SELECT * FROM records WHERE id = ?", (int(rec_id),))
        record = cursor.fetchone()
        # then printed each field
    
    Django equivalent: get_object_or_404(Record, id=record_id)
    Automatically returns 404 if not found (replaces the 
    "Record not found" error message).
    """
    record = get_object_or_404(Record, id=record_id)
    return render(request, 'vault/detail.html', {
        'record': record
    })


def add_record(request):
    """
    Replaces: add_record() which used input() prompts.
    
    Original flow:
        domain = input("Domain: ")
        url = input("Full URL: ")
        ...
        cursor.execute("INSERT INTO records ... VALUES (?, ?, ...)")
    
    Django flow:
        form = RecordForm(request.POST)
        if form.is_valid():
            form.save()  # Handles INSERT automatically
    """
    if request.method == 'POST':
        form = RecordForm(request.POST)
        if form.is_valid():
            record = form.save()
            messages.success(
                request,
                _('Record successfully added with ID: %(record_id)s')
                % {'record_id': record.id}
            )
            return redirect('vault:detail', record_id=record.id)
    else:
        form = RecordForm()

    return render(request, 'vault/add.html', {'form': form})


def update_record(request, record_id):
    """
    Replaces: update_record() which fetched a record, showed current 
    values, and accepted new input() for each field.
    
    Original:
        cursor.execute("SELECT * FROM records WHERE id = ?", ...)
        domain = input(f"Domain [{record['domain']}]: ") or record["domain"]
        cursor.execute("UPDATE records SET ... WHERE id = ?", ...)
    
    Django: ModelForm with instance= pre-fills current values.
    form.save() handles the UPDATE query.
    """
    record = get_object_or_404(Record, id=record_id)

    if request.method == 'POST':
        form = RecordForm(request.POST, instance=record)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                _('Record #%(record_id)s updated successfully.')
                % {'record_id': record_id}
            )
            return redirect('vault:detail', record_id=record.id)
    else:
        # instance=record pre-fills the form with current values
        # This replaces showing "[current_value]" in input prompts
        form = RecordForm(instance=record)

    return render(request, 'vault/update.html', {
        'form': form,
        'record': record
    })


def delete_record(request, record_id):
    """
    Replaces: delete_record() which asked for confirmation via input().
    
    Original:
        confirm = input(f"Are you sure? (y/N): ")
        if confirm == "y":
            cursor.execute("DELETE FROM records WHERE id = ?", ...)
    
    Django: POST request serves as confirmation.
    GET shows the confirmation page.
    """
    record = get_object_or_404(Record, id=record_id)

    if request.method == 'POST':
        record_id_display = record.id
        record.delete()
        messages.success(
            request,
            _('Record #%(record_id)s deleted successfully.')
            % {'record_id': record_id_display}
        )
        return redirect('vault:browse')

    return render(request, 'vault/delete_confirm.html', {
        'record': record
    })


def import_from_file(request):
    """
    Replaces: import_from_file() which read from a local file path.
    
    Original:
        filepath = input("[?] Enter path to the data file: ")
        with open(filepath, "r") as f:
            for line in f:
                record = parse_line(line)
                ...
        cursor.executemany("INSERT INTO records ... VALUES (...)", parsed)
    
    Django: User uploads file through browser.
    File content is read from request.FILES.
    bulk_create() replaces executemany().
    """
    if request.method == 'POST':
        form = ImportFileForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES['file']
            parsed_records = []
            skipped_count = 0

            # Read the uploaded file line by line
            # (replaces: with open(filepath, "r") as f)
            for line in uploaded_file:
                try:
                    decoded_line = line.decode('utf-8', errors='ignore')
                except AttributeError:
                    decoded_line = str(line)

                result = parse_line(decoded_line)
                if result:
                    parsed_records.append(Record(**result))
                elif decoded_line.strip():
                    skipped_count += 1

            if not parsed_records:
                messages.warning(
                    request,
                    _('No valid records found in the file.')
                )
            else:
                # bulk_create replaces cursor.executemany()
                Record.objects.bulk_create(parsed_records)
                messages.success(
                    request,
                    ngettext(
                        'Successfully imported %(count)s record.',
                        'Successfully imported %(count)s records.',
                        len(parsed_records),
                    )
                    % {'count': len(parsed_records)}
                    + ' '
                    + _('Filtered out %(count)s non-record lines.')
                    % {'count': skipped_count}
                )
                return redirect('vault:browse')
    else:
        form = ImportFileForm()

    return render(request, 'vault/import.html', {'form': form})


def export_csv(request):
    """
    Replaces: export_data() CSV branch.
    
    Original:
        with open("vault_export.csv", "w") as f:
            writer = csv.writer(f)
            ...
    
    Django: Instead of writing to a local file, we send the CSV
    directly to the user's browser as a download.
    """
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        'attachment; filename="vault_export.csv"'
    )

    writer = csv.writer(response)
    writer.writerow([
        _('ID'), _('Domain'), _('URL'), _('Account Type'),
        _('Identifier'), _('Secret Data'), _('IP Address')
    ])

    for r in Record.objects.all():
        writer.writerow([
            r.id, r.domain, r.url, r.account_type,
            r.account_identifier, r.secret_data, r.ip_address
        ])

    return response


def export_json(request):
    """
    Replaces: export_data() JSON branch.
    
    Original:
        data_list = [dict(r) for r in rows]
        with open("vault_export.json", "w") as f:
            json.dump(data_list, f, indent=4)
    
    Django: Sends JSON directly to the browser as a download.
    """
    records = list(Record.objects.values(
        'id', 'domain', 'url', 'account_type',
        'account_identifier', 'secret_data', 'ip_address'
    ))

    response = HttpResponse(
        json.dumps(records, indent=4, ensure_ascii=False),
        content_type='application/json'
    )
    response['Content-Disposition'] = (
        'attachment; filename="vault_export.json"'
    )
    return response