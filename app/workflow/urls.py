from django.urls import path

from . import views, views_bulk

app_name = 'workflow'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('guide/', views.guide, name='guide'),
    path('admin-tools/reset-test-data/', views.reset_test_data, name='reset_test_data'),
    path('requests/new/', views.new_request, name='new_request'),
    path('requests/<int:pk>/', views.request_detail, name='request_detail'),
    path('notifications/', views.notifications, name='notifications'),
    path('bulk/', views_bulk.bulk_home, name='bulk'),
    path('bulk/mapping-template/', views_bulk.bulk_mapping_template, name='bulk_mapping_template'),
    path('bulk/upload/', views_bulk.bulk_upload, name='bulk_upload'),
    path('bulk/download/', views_bulk.bulk_download, name='bulk_download'),
]
