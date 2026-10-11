
from django.urls import path
from . import views

app_name = 'vault'

"""
This replaces the if/elif chain in main_menu():

    if choice == "1":   browse_all()       →  /records/
    elif choice == "2": search_records()   →  /search/
    elif choice == "3": view_detail()      →  /records/<id>/
    elif choice == "4": import_from_file() →  /import/
    elif choice == "5": add_record()       →  /records/add/
    elif choice == "6": update_record()    →  /records/<id>/edit/
    elif choice == "7": delete_record()    →  /records/<id>/delete/
    elif choice == "8": export_data()      →  /export/csv/ or /export/json/
"""

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('records/', views.browse_all, name='browse'),
    path('records/add/', views.add_record, name='add'),
    path('records/<int:record_id>/', views.view_detail, name='detail'),
    path('records/<int:record_id>/edit/', views.update_record, name='update'),
    path('records/<int:record_id>/delete/', views.delete_record, name='delete'),
    path('search/', views.search_records, name='search'),
    path('import/', views.import_from_file, name='import'),
    path('export/csv/', views.export_csv, name='export_csv'),
    path('export/json/', views.export_json, name='export_json'),
]