from django.contrib import admin

from .models import Account, Email, Ip, Pass, Url, User


@admin.register(Url)
class UrlAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Pass)
class PassAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Ip)
class IpAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'url', 'account_type', 'identifier', 'ip', 'created_at'
    ]
    list_filter = ['account_type']
    list_select_related = ['url', 'email', 'user', 'ip']
    autocomplete_fields = ['url', 'email', 'user', 'password', 'ip']
    search_fields = [
        'url__name', 'email__name', 'user__name',
        'password__name', 'ip__name'
    ]


from .models import ImportJob, ImportRecord, ImportRecoveryLog


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = [
        'job_id', 'source_name', 'status', 'stage',
        'total_committed', 'total_skipped', 'total_failed',
        'created_at', 'completed_at',
    ]
    list_filter = ['status', 'stage']
    search_fields = ['source_name', 'source_path']
    readonly_fields = [
        'job_id', 'created_at', 'started_at', 'last_activity_at',
        'completed_at', 'worker_id', 'lease_expires_at',
    ]


@admin.register(ImportRecord)
class ImportRecordAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'import_job', 'source_position', 'status',
        'account', 'processed_at',
    ]
    list_filter = ['status']
    search_fields = ['source_line', 'error_message']
    readonly_fields = ['idempotency_key']


@admin.register(ImportRecoveryLog)
class ImportRecoveryLogAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'import_job', 'attempt_number', 'outcome',
        'recovery_started_at', 'recovery_completed_at',
    ]
    list_filter = ['outcome']
