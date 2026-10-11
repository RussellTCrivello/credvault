
from django.contrib import admin
from .models import Record


@admin.register(Record)
class RecordAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'domain', 'account_type',
        'account_identifier', 'ip_address'
    ]
    list_filter = ['account_type']
    search_fields = [
        'domain', 'url', 'account_identifier',
        'secret_data', 'ip_address'
    ]