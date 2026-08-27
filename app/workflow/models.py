import uuid

from django.conf import settings
from django.db import models

from catalog.models import PackagingFile, Product, sanitize_filename_part


_NOTIFY_STATUS_LABEL_KO = {
    'sent': '발송 성공',
    'failed': '발송 실패',
    'mock': '모의 — 실제 미발송',
    'no_recipients': '수신자 없음',
}


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
        # 셀렉트 목록 노출 순서 — 자주 쓰는 사유가 위로, "그 외 기타"는 맨 아래로.
        NEEDS_REVISION = 'NEEDS_REVISION', '디자인 수정 요청'
        BULK_UPLOAD = 'BULK_UPLOAD', '파일 확인 요청'
        STOCK_SHORTAGE = 'STOCK_SHORTAGE', '그 외 기타'

    request_no = models.CharField(max_length=20, unique=True, editable=False,
                                   help_text='자동 채번되는 요청번호 (예: RQ-20260722-001)')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='requests')
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='requests_made')
    reason = models.CharField(max_length=20, choices=Reason.choices)
    detail = models.TextField(blank=True, help_text='요청자가 남긴 구체적인 요청사항(선택)')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REVIEW1)
    current_file = models.ForeignKey(PackagingFile, null=True, blank=True, on_delete=models.SET_NULL, related_name='requests')
    used_exception = models.BooleanField(
        default=False,
        help_text='연구소 최종검수를 생략하고 완료했는지 여부 — 3개월 이내 승인 예외, 또는 1차 검토·관리 창구의 직접 완료 처리')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '발주 건'
        verbose_name_plural = '발주 건'

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

    # 대시보드 "진행 흐름" 표시용. 예전에는 1차검토 하나에 요청접수·내부확인·디자인수정이
    # 뭉뚱그려져 있어서, 목록만 봐서는 지금 실제로 누가 무엇을 붙들고 있는지 알 수 없었다.
    # 실제 흐름대로 단계를 쪼개고, 단계마다 담당을 함께 내보내 화면에서 안내(hover)한다.
    # (label, 담당, 이 단계에 해당하는 status)
    _FLOW_STEPS = [
        ('요청접수', '울산공장', None),
        ('내부확인', '브랜드기획팀', 'REVIEW1'),
        ('디자인수정', '디자인팀', 'DESIGN_EDIT'),
        ('최종검수', '연구소', 'FINAL_REVIEW'),
        ('전달대기', '브랜드기획팀', 'APPROVED'),
        ('완료', None, 'COMPLETED'),
    ]
    _FLOW_CURRENT_INDEX = {
        'SUBMITTED': 1,
        'REVIEW1': 1,
        'DESIGN_EDIT': 2,
        'FINAL_REVIEW': 3,
        'APPROVED': 4,
        'COMPLETED': 5,
    }

    def _design_edit_happened(self):
        """디자인수정 단계를 실제로 거쳤는지. 모든 건이 거치는 단계가 아니라서, 지나갔다고
        무조건 "완료"로 칠하면 사실과 다르다. events가 prefetch되어 있으면 추가 쿼리는 없다."""
        edit_actions = {RequestEvent.Action.REVIEW_REQUEST_EDIT, RequestEvent.Action.DESIGN_UPLOADED}
        return any(e.action in edit_actions for e in self.events.all())

    def flow_progress(self):
        status = self.status
        cancelled = status == self.Status.CANCELLED
        completed = status == self.Status.COMPLETED
        current_index = self._FLOW_CURRENT_INDEX.get(status, 1)
        design_done = self._design_edit_happened()

        steps = []
        for i, (label, owner, _st) in enumerate(self._FLOW_STEPS):
            if cancelled:
                state = 'cancelled'
            elif completed or i < current_index:
                state = 'done'
            elif i == current_index:
                state = 'current'
            else:
                state = 'pending'

            # 거치지 않고 지나간 디자인수정은 "완료"가 아니라 "해당 없음"으로 구분한다.
            if label == '디자인수정' and state == 'done' and not design_done:
                state = 'skipped'

            if state == 'current':
                tooltip = f'지금 {owner} 처리 대기 중입니다' if owner else '완료 처리 단계입니다'
            elif state == 'done':
                tooltip = f'{label} 완료'
            elif state == 'skipped':
                tooltip = '디자인 수정 없이 진행된 건입니다'
            elif state == 'cancelled':
                tooltip = '취소된 건입니다'
            else:
                tooltip = f'{owner} 차례 (예정)' if owner else '예정'

            steps.append({'label': label, 'state': state, 'owner': owner, 'tooltip': tooltip})

        # 목록에서 한 줄로 "지금 누구 차례인지" 바로 읽히도록 따로 내보낸다.
        current_owner = None
        if not cancelled and not completed:
            current_owner = self._FLOW_STEPS[current_index][1]

        return {
            'steps': steps,
            'cancelled': cancelled,
            'completed': completed,
            'current_owner': current_owner,
            'current_label': self._FLOW_STEPS[current_index][0],
        }


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
        REVIEW_DIRECT_COMPLETE = 'REVIEW_DIRECT_COMPLETE', '연구소 검수 없이 완료 처리'
        BULK_UPLOAD = 'BULK_UPLOAD', '일괄 업로드로 파일 강제 갱신'
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
    # NOTIFY 건에서만 채워진다 — 'sent'|'failed'|'mock'|'no_recipients' (notify.py가 반환하는
    # status 그대로). 값이 비어있으면(과거 이력 포함) 그 채널은 아예 시도하지 않았거나
    # 아직 이 필드가 생기기 전 이력이라는 뜻 — 타임라인에서 배지를 표시하지 않는다.
    email_status = models.CharField(max_length=20, blank=True)
    push_status = models.CharField(max_length=20, blank=True)
    kakao_status = models.CharField(max_length=20, blank=True)
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

    @property
    def notify_badges(self):
        """알림 발송(NOTIFY) 이력을 타임라인에 '이메일 ●, 카카오톡/문자 X'처럼 간단히
        보여주기 위한 배지 목록. status가 비어있으면(그 채널을 아예 시도하지 않았거나
        이 필드가 생기기 전의 과거 이력) 배지 자체를 만들지 않는다."""
        badges = []
        for label, status in (
            ('이메일', self.email_status),
            ('브라우저 알림', self.push_status),
            ('카카오톡/문자', self.kakao_status),
        ):
            if not status:
                continue
            badges.append({'label': label, 'ok': status == 'sent', 'status_label': _NOTIFY_STATUS_LABEL_KO.get(status, status)})
        return badges


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
