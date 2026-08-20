from django.contrib import admin
from django.db import transaction
from django.db.models import ProtectedError

from .models import PackagingFile, Product


@admin.action(description='선택 항목 숨기기 (목록/검색/신규요청에서 제외, 데이터는 유지)')
def hide_selected(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    modeladmin.message_user(request, f'{updated}건을 숨김 처리했습니다.')


@admin.action(description='선택 항목 숨김 해제')
def unhide_selected(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    modeladmin.message_user(request, f'{updated}건을 다시 표시했습니다.')


@admin.action(description='⚠ 완전 삭제 (복구 불가 — 관리자 전용)')
def hard_delete_products(modeladmin, request, queryset):
    """품목과 그에 딸린 파일(버전 이력 포함, 저장소의 실제 파일까지)을 영구 삭제한다.
    재발주 이력이 하나라도 남아있는 품목은 PROTECT 제약으로 삭제가 막히며, 그 목록을
    사용자에게 알려준다(먼저 해당 이력을 정리해야 함)."""
    if not request.user.is_superuser:
        modeladmin.message_user(request, '이 작업은 관리자 계정만 실행할 수 있습니다.', level='ERROR')
        return

    deleted, blocked = [], []
    for product in queryset:
        try:
            with transaction.atomic():
                for pkg in product.files.all():
                    if pkg.ai_file:
                        pkg.ai_file.delete(save=False)
                    if pkg.jpg_file:
                        pkg.jpg_file.delete(save=False)
                label = f'{product.code} {product.name}'
                product.delete()
                deleted.append(label)
        except ProtectedError:
            blocked.append(f'{product.code} {product.name} (연결된 재발주 건이 있어 삭제 불가)')

    if deleted:
        modeladmin.message_user(request, f'완전 삭제됨: {", ".join(deleted)}')
    if blocked:
        modeladmin.message_user(
            request, f'삭제되지 않음(재발주 이력 존재): {", ".join(blocked)}', level='WARNING')


@admin.action(description='⚠ 완전 삭제 (복구 불가 — 관리자 전용)')
def hard_delete_files(modeladmin, request, queryset):
    if not request.user.is_superuser:
        modeladmin.message_user(request, '이 작업은 관리자 계정만 실행할 수 있습니다.', level='ERROR')
        return

    deleted = 0
    for pkg in queryset:
        if pkg.ai_file:
            pkg.ai_file.delete(save=False)
        if pkg.jpg_file:
            pkg.jpg_file.delete(save=False)
        pkg.delete()
        deleted += 1
    modeladmin.message_user(request, f'{deleted}건을 완전 삭제했습니다.')


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
    actions = [hide_selected, unhide_selected, hard_delete_products]

    def has_delete_permission(self, request, obj=None):
        # 실수로 완전 삭제되는 걸 막는다 — 개별 삭제 링크 대신 위 액션(체크박스 선택 +
        # 명시적 확인)을 통해서만, 그것도 관리자 계정으로만 완전 삭제할 수 있게 한다.
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop('hard_delete_products', None)
        return actions


@admin.register(PackagingFile)
class PackagingFileAdmin(admin.ModelAdmin):
    list_display = ('product', 'version', 'status', 'is_active', 'uploaded_by', 'uploaded_at', 'approved_at')
    list_filter = ('status', 'is_active')
    search_fields = ('product__name', 'product__code')
    actions = [hide_selected, unhide_selected, hard_delete_files]

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop('hard_delete_files', None)
        return actions
