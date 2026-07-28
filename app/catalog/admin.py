from django.contrib import admin

from .models import PackagingFile, Product


@admin.action(description='선택 항목 숨기기 (목록/검색/신규요청에서 제외, 데이터는 유지)')
def hide_selected(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    modeladmin.message_user(request, f'{updated}건을 숨김 처리했습니다.')


@admin.action(description='선택 항목 숨김 해제')
def unhide_selected(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    modeladmin.message_user(request, f'{updated}건을 다시 표시했습니다.')


class PackagingFileInline(admin.TabularInline):
    model = PackagingFile
    extra = 0
    can_delete = False
    readonly_fields = ('version', 'uploaded_by', 'uploaded_at', 'approved_by', 'approved_at')
    fields = ('version', 'status', 'is_active', 'ai_file', 'jpg_file', 'note',
              'uploaded_by', 'uploaded_at', 'approved_by', 'approved_at')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'category', 'product_line', 'is_active', 'created_at')
    list_filter = ('category', 'product_line', 'is_active')
    search_fields = ('code', 'name')
    inlines = [PackagingFileInline]
    actions = [hide_selected, unhide_selected]

    def has_delete_permission(self, request, obj=None):
        # 실수로 완전 삭제되는 걸 막는다 — "삭제" 대신 항상 숨기기 액션을 쓰도록 유도.
        return False


@admin.register(PackagingFile)
class PackagingFileAdmin(admin.ModelAdmin):
    list_display = ('product', 'version', 'status', 'is_active', 'uploaded_by', 'uploaded_at', 'approved_at')
    list_filter = ('status', 'is_active')
    search_fields = ('product__name', 'product__code')
    actions = [hide_selected, unhide_selected]

    def has_delete_permission(self, request, obj=None):
        return False
