from django.conf import settings
from django.db import models

from catalog.models import PackagingFile, Product


class ReorderRequest(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = 'SUBMITTED', '요청등록'
        REVIEW1 = 'REVIEW1', '1차검토중'
        DESIGN_EDIT = 'DESIGN_EDIT', '디자인수정중'
        FINAL_REVIEW = 'FINAL_REVIEW', '최종검수중'
        APPROVED = 'APPROVED', '승인(전달 대기)'
        COMPLETED = 'COMPLETED', '완료'

    TERMINAL_STATUSES = {Status.COMPLETED}

    class Reason(models.TextChoices):
        STOCK_SHORTAGE = 'STOCK_SHORTAGE', '재고 소진(임박)'
        NEEDS_REVISION = 'NEEDS_REVISION', '표시사항 등 수정 필요'

    request_no = models.CharField(max_length=20, unique=True, editable=False,
                                   help_text='자동 채번되는 요청번호 (예: RQ-20260722-001)')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='requests')
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='requests_made')
    reason = models.CharField(max_length=20, choices=Reason.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REVIEW1)
    current_file = models.ForeignKey(PackagingFile, null=True, blank=True, on_delete=models.SET_NULL, related_name='requests')
    used_exception = models.BooleanField(default=False, help_text='3개월 이내 승인 예외로 최종검수를 생략했는지 여부')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '재발주 건'
        verbose_name_plural = '재발주 건'

    def __str__(self):
        return f'{self.request_no} {self.product.name} ({self.get_status_display()})'

    def status_pill_class(self):
        return {
            self.Status.SUBMITTED: 'pill-submitted',
            self.Status.REVIEW1: 'pill-review1',
            self.Status.DESIGN_EDIT: 'pill-design',
            self.Status.FINAL_REVIEW: 'pill-final',
            self.Status.APPROVED: 'pill-approved',
            self.Status.COMPLETED: 'pill-completed',
        }[self.Status(self.status)]


class RequestEvent(models.Model):
    class Action(models.TextChoices):
        SUBMITTED = 'SUBMITTED', '요청 등록'
        REVIEW_TO_FINAL = 'REVIEW_TO_FINAL', '최종본 확인 → 최종검수 요청'
        REVIEW_REQUEST_EDIT = 'REVIEW_REQUEST_EDIT', '수정 요청'
        DESIGN_UPLOADED = 'DESIGN_UPLOADED', '디자인파일 수정 업로드'
        FINAL_APPROVE = 'FINAL_APPROVE', '최종 승인'
        FINAL_REVISION = 'FINAL_REVISION', '수정 필요(경미)'
        FINAL_REJECT = 'FINAL_REJECT', '반려'
        HANDOFF = 'HANDOFF', '최종파일 전달 · 완료'
        EXCEPTION_SKIP = 'EXCEPTION_SKIP', '3개월 예외 적용(최종검수 생략)'
        NOTIFY = 'NOTIFY', '알림 발송'

    class Channel(models.TextChoices):
        SYSTEM = 'SYSTEM', '시스템'
        EMAIL_MOCK = 'EMAIL_MOCK', '이메일'
        KAKAO_MOCK = 'KAKAO_MOCK', '카카오톡/문자(모의)'

    request = models.ForeignKey(ReorderRequest, on_delete=models.CASCADE, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    action = models.CharField(max_length=30, choices=Action.choices)
    note = models.TextField(blank=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.SYSTEM)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = '이력'
        verbose_name_plural = '이력'

    def __str__(self):
        return f'{self.request_id} · {self.get_action_display()}'


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    request = models.ForeignKey(ReorderRequest, on_delete=models.CASCADE, related_name='notifications')
    message = models.CharField(max_length=255)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '알림'
        verbose_name_plural = '알림'

    def __str__(self):
        return self.message
