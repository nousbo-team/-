from django.contrib import admin

from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'department', 'is_away', 'backup_user')
    list_filter = ('role', 'is_away')
    search_fields = ('user__username', 'user__first_name', 'department')
