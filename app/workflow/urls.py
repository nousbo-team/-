from django.urls import path

from . import views, views_bulk

app_name = 'workflow'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('history/', views.history, name='history'),
    path('guide/', views.guide, name='guide'),
    path('admin-tools/reset-test-data/', views.reset_test_data, name='reset_test_data'),
    path('admin-tools/seed-demo/', views.seed_demo, name='seed_demo'),
    path('requests/new/', views.new_request, name='new_request'),
    path('requests/<int:pk>/', views.request_detail, name='request_detail'),
    path('attachments/<int:pk>/', views.download_attachment, name='download_attachment'),
    path('notifications/', views.notifications, name='notifications'),
    path('assistant/', views.assistant_page, name='assistant'),
    path('assistant/ask/', views.assistant_ask, name='assistant_ask'),
    path('assistant/ask/stream/', views.assistant_ask_stream, name='assistant_ask_stream'),
    path('assistant/diagnostics/', views.assistant_diagnostics, name='assistant_diagnostics'),
    path('bulk/', views_bulk.bulk_home, name='bulk'),
    path('bulk/history/', views_bulk.bulk_upload_history, name='bulk_upload_history'),
    path('bulk/upload/', views_bulk.bulk_upload, name='bulk_upload'),
    path('bulk/download/', views_bulk.bulk_download, name='bulk_download'),
    path('sw.js', views.service_worker, name='service_worker'),
    path('push/subscribe/', views.push_subscribe, name='push_subscribe'),
    path('push/unsubscribe/', views.push_unsubscribe, name='push_unsubscribe'),
]
