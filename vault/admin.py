
from django.contrib import admin

from .models import Account, Email, Ip, Pass, Url, User


class ReferenceAdmin(admin.ModelAdmin):
    """Shared behaviour for the five value tables."""

    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']
    # With millions of values a page of 100 keeps the changelist honest.
    list_per_page = 100
    ordering = ['name']


@admin.register(Url)
class UrlAdmin(ReferenceAdmin):
    list_display = ['name', 'host', 'created_at', 'updated_at']
    readonly_fields = ['host', 'created_at', 'updated_at']


@admin.register(Email)
class EmailAdmin(ReferenceAdmin):
    pass


@admin.register(User)
class UserAdmin(ReferenceAdmin):
    pass


@admin.register(Pass)
class PassAdmin(ReferenceAdmin):
    """Secrets are searchable by exact name; the list view shows them as-is."""


@admin.register(Ip)
class IpAdmin(ReferenceAdmin):
    pass


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'url', 'account_type', 'identifier', 'ip',
        'created_at', 'updated_at',
    ]
    list_filter = ['account_type']
    list_select_related = ['url', 'email', 'user', 'ip']
    search_fields = [
        'url__name', 'url__host', 'email__name', 'user__name',
        'password__name', 'ip__name',
    ]
    list_per_page = 100
    date_hierarchy = 'created_at'

    @admin.display(description='Identifier', ordering='email__name')
    def identifier(self, obj):
        return obj.identifier
