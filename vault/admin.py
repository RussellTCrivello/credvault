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
    search_fields = [
        'url__name', 'email__name', 'user__name',
        'password__name', 'ip__name'
    ]
