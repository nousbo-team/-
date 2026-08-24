import uuid

from django.conf import settings
from django.db import models

from catalog.models import PackagingFile, Product, sanitize_filename_part


def request_attachment_upload_to(instance, filename):
    # catalog.models.packaging_upload_to와 같은 이유 — 원본 파일명(한글 등)을 그대로
    # 저장 키로 쓰면 Supabase Storage가 "Invalid key"로 거부해 업로드가 500으로
    # 실패한다. 확장자만 남기고 나머지는 ASCII-safe한 랜덤 값으로 대체한다.
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    return f'request_attachments/{instance.request_id}/{uuid.uuid4().hex}.{ext}'


class ReorderRequest(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = 'SUBMITTED', '요청등록'
        REVIEW1 = 'REVIEW1', '1차검토중'
        DESIGN_EDIT = 'DESIGN_EDIT', '디자인수정중'
        FINAL_REVIEW = 'FINAL_REVIEW', '최종검수중'
        APPROVED = 'APPROVED', '승인(전달 대기)'
        COMPLETED = 'COMPLETED', '완료'
        CANCELLED = 'CANCELLED', '취소됨'

    TERMINAL_STATUSES = {Status.COMPLETED, Status.CANCELLED}

    class Reason(models.TextChoices):
        STOCK_SHORTAGE = 'STOCK_SHORTAGE', '재고 소진(임박)'
        NEEDS_REVISION = 'NEEDS_REVISION', '표시사항 등 수정 필요'

    request_no = models.CharField(max_length=20, unique=True, editable=False,
                                   help_text='자동 채번되는 요청번호 (예: RQ-20260722-001)')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='requests')
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='requests_made')
    reason = models.CharField(max_length=20, choices=Reason.choices)
    detail = models.TextField(blank=True, help_text='요청자가 남긴 구체적인 요청사항(선택)')
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
            self.Status.CANCELLED: 'pill-rejected',
        }[self.Status(self.status)]

    # 대시보드에 "지금 어느 단계인지" 한눈에 보여주기 위한 간단한 흐름 표시(P0-5 보강).
    # 실제 이력(RequestEvent)을 조회하지 않고 현재 status만으로 계산하는 가벼운 요약이라,
    # 디자인수정 루프를 여러 번 거쳤어도 "지금" 기준으로만 위치를 보여준다.
    _FLOW_ORDER = [Status.REVIEW1, Status.FINAL_REVIEW, Status.APPROVED, Status.COMPLETED]
    _FLOW_LABELS = ['1차검토', '최종검수', '전달대기', '완료']

    def flow_progress(self):
        status = self.Status(self.status)
        cancelled = status == self.Status.CANCELLED
        completed = status == self.Status.COMPLETED
        design_edit = status == self.Status.DESIGN_EDIT

        if cancelled:
            current_index = -1
        elif design_edit:
            current_index = 0  # 디자인수정은 1차검토 루프 중이므로 그 위치에 표시
        else:
            try:
                current_index = self._FLOW_ORDER.index(status)
            except ValueError:
                current_index = 0

        steps = []
        for i, label in enumerate(self._FLOW_LABELS):
            if cancelled:
                state = 'cancelled'
            elif completed:
                state = 'done'
            elif i < current_index:
                state = 'done'
            elif i == current_index:
                state = 'current'
            else:
                state = 'pending'
            steps.append({'label': label, 'state': state})

        return {'steps': steps, 'design_edit': design_edit, 'cancelled': cancelled}


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
        CANCELLED = 'CANCELLED', '요청 취소'
        DESIGN_REJECT = 'DESIGN_REJECT', '디자인 반려(1차검토로)'
        APPROVAL_REVERTED = 'APPROVAL_REVERTED', '승인 취소(최종검수로 되돌림)'

    class Channel(models.TextChoices):
        SYSTEM = 'SYSTEM', '시스템'
        EMAIL_MOCK = 'EMAIL_MOCK', '이메일'
        KAKAO_MOCK = 'KAKAO_MOCK', '카카오톡/문자(모의)'

    request = models.ForeignKey(ReorderRequest, on_delete=models.CASCADE, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    action = models.CharField(max_length=30, choices=Action.choices)
    note = models.TextField(blank=True)
    attachment = models.FileField(
        upload_to=request_attachment_upload_to, null=True, blank=True,
        help_text='표시사항 가이드 등 지시사항에 첨부하는 참고 파일(선택) — 예: 1차검토에서 디자인 수정 요청 시')
    attachment_original_name = models.CharField(
        max_length=255, blank=True,
        help_text='원본 파일명(저장 키는 한글 등을 피해 랜덤값으로 바뀌므로, 화면 표시용으로 따로 보관)')
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.SYSTEM)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = '이력'
        verbose_name_plural = '이력'

    def __str__(self):
        return f'{self.request_id} · {self.get_action_display()}'

    @property
    def attachment_filename(self):
        """다운로드 파일명 규칙: 요청번호_참고파일_날짜.확장자 (원본 파일명은
        attachment_original_name에 별도 보관하되, 실제 저장·표시되는 이름은 이
        규칙을 따른다 — 포장지 AI/JPG 파일과 동일한 정책)."""
        if not self.attachment:
            return ''
        source_name = self.attachment_original_name or self.attachment.name
        ext = source_name.rsplit('.', 1)[-1].lower() if '.' in source_name else 'bin'
        date_str = self.created_at.strftime('%Y%m%d')
        req_no = sanitize_filename_part(self.request.request_no)
        return f'{req_no}_참고파일_{date_str}.{ext}'


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


class PushSubscription(models.Model):
    """브라우저의 웹 푸시 구독 정보(기기·브라우저별로 하나씩) — Notification 생성 시
    이걸 갖고 있는 사용자에게 실제 브라우저 알림을 보낸다."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '웹 푸시 구독'
        verbose_name_plural = '웹 푸시 구독'

    def __str__(self):
        return f'{self.user} · {self.endpoint[:40]}...'
