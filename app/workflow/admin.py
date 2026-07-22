from django.contrib import admin

from .models import Notification, RequestEvent, ReorderRequest


class RequestEventInline(admin.TabularInline):
    model = RequestEvent
    extra = 0
    readonly_fields = ('actor', 'action', 'note', 'channel', 'created_at')


@admin.register(ReorderRequest)
class ReorderRequestAdmin(admin.ModelAdmin):
    list_display = ('request_no', 'product', 'requester', 'status', 'reason', 'used_exception', 'created_at')
    list_filter = ('status', 'reason', 'used_exception')
    search_fields = ('request_no', 'product__name', 'product__code')
    inlines = [RequestEventInline]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'request', 'message', 'is_read', 'created_at')
    list_filter = ('is_read',)
