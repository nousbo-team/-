from django.urls import path

from . import views

app_name = 'catalog'

urlpatterns = [
    path('', views.product_list, name='product_list'),
    path('import-master/', views.import_master_list, name='import_master'),
    path('files/<int:pk>/<str:field>/', views.download_packaging_file, name='download_packaging_file'),
    path('<int:pk>/', views.product_detail, name='product_detail'),
]
