from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    class Role(models.TextChoices):
        REQUESTER = 'REQUESTER', '요청자 (공장)'
        REVIEWER = 'REVIEWER', '1차 검토·관리 창구 (브랜드기획팀)'
        DESIGNER = 'DESIGNER', '디자인'
        APPROVER = 'APPROVER', '최종 검수·반려 판단 (연구소)'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=Role.choices)
    department = models.CharField(max_length=50, blank=True)
    title = models.CharField(max_length=50, blank=True)
    is_away = models.BooleanField(default=False, help_text='부재중으로 설정하면 backup_user가 동일 권한으로 처리할 수 있다')
    backup_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='backing_up_for',
        help_text='이 사람이 부재중일 때 대신 처리할 담당자',
    )

    class Meta:
        verbose_name = '담당자 프로필'
        verbose_name_plural = '담당자 프로필'

    def __str__(self):
        return f'{self.user.get_full_name() or self.user.username} ({self.get_role_display()})'

    def active_handlers(self):
        """이 담당자 역할의 대기건을 볼 수 있는 사용자 목록(본인 + 부재중일 때 backup)."""
        users = [self.user]
        if self.is_away and self.backup_user_id:
            users.append(self.backup_user)
        return users
