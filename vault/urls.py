from django.contrib.auth.views import LogoutView
from django.urls import path
from . import views
from .access import AdministratorLoginView
app_name = 'vault'

"""
This replaces the if/elif chain in main_menu():

    if choice == "1":   import_from_file()  →  /import/
    elif choice == "2": add_record()        →  /records/add/
    elif choice == "3": search_records()    →  /search/
    elif choice == "4": browse_all()        →  /records/
    elif choice == "5": view_detail()       →  /records/<id>/
    elif choice == "6": update_record()     →  /records/<id>/edit/
    elif choice == "7": delete_record()     →  /records/<id>/delete/
    elif choice == "8": export_data()       →  /export/csv/ or /export/json/

Plus the normalized relationship management:
    /references/<kind>/                 →  reference_list
    /references/<kind>/delete/<id>/     →  reference_delete
    /advanced-search/                   →  advanced_search
"""

urlpatterns = [
    path('login/', AdministratorLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(next_page='vault:login'), name='logout'),
    path('', views.dashboard, name='dashboard'),
    path('records/', views.browse_all, name='browse'),
    path('records/add/', views.add_account, name='add'),   path('records/<int:pk>/', views.view_detail, name='detail'),
    path('records/<int:pk>/edit/', views.update_account, name='update'),
    path('records/<int:pk>/delete/', views.delete_account, name='delete'),
    path('search/', views.search_records, name='search'),
    path('advanced-search/', views.advanced_search, name='advanced_search'),
    path('references/', views.references_index, name='references_index'),
    path('references/<str:kind>/', views.reference_list, name='references'),
    path('references/<str:kind>/suggest/',
         views.reference_suggest, name='reference_suggest'),
    path('references/<str:kind>/delete/<int:pk>/',
         views.reference_delete, name='reference_delete'),
    path('import/', views.import_from_file, name='import'),
    path('settings/', views.settings_view, name='settings'),
    path('search/api/', views.search_api, name='search_api'),
    path('export/csv/', views.export_csv, name='export_csv'),
    path('export/json/', views.export_json, name='export_json'),
]
