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
    phone_number = models.CharField(
        max_length=20, blank=True,
        help_text='카카오톡/문자 알림 수신용 휴대폰번호 (예: 01012345678, - 없이). 비워두면 해당 담당자는 카카오톡/문자를 받지 않음(이메일·인앱 알림은 그대로 감).',
    )
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


def get_profile(user):
    """user.profile을 안전하게 조회한다. nousbo 같은 슈퍼유저는 업무 프로필이
    없을 수 있는데, `request.user.profile`을 직접 쓰면 RelatedObjectDoesNotExist가
    그대로 터진다(getattr의 기본값 처리로는 못 막음 — 평가 자체가 이미 실패하기 때문).
    반드시 이 함수를 통해 조회할 것."""
    return getattr(user, 'profile', None)
