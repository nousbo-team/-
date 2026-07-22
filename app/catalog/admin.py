from django.contrib import admin

from .models import PackagingFile, Product


class PackagingFileInline(admin.TabularInline):
    model = PackagingFile
    extra = 0
    readonly_fields = ('version', 'uploaded_by', 'uploaded_at', 'approved_by', 'approved_at')
    fields = ('version', 'status', 'ai_file', 'jpg_file', 'note', 'uploaded_by', 'uploaded_at', 'approved_by', 'approved_at')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'category', 'product_line', 'created_at')
    list_filter = ('category', 'product_line')
    search_fields = ('code', 'name')
    inlines = [PackagingFileInline]


@admin.register(PackagingFile)
class PackagingFileAdmin(admin.ModelAdmin):
    list_display = ('product', 'version', 'status', 'uploaded_by', 'uploaded_at', 'approved_at')
    list_filter = ('status',)
    search_fields = ('product__name',)
