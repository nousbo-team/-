import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# 다운로드 파일명에 그대로 못 쓰는 문자(Windows 기준 예약 문자) — 저장 키가 아니라
# 화면에 보여주고 실제로 저장될 파일명을 만들 때 공통으로 쓰는 치환 규칙이다.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename_part(text):
    """다운로드 파일명 한 조각을 OS에서 안전하게 쓸 수 있도록 정리한다."""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub('_', text).strip()
    return cleaned or 'file'


def packaging_upload_to(instance, filename):
    # 원본 파일명(한글 등)을 그대로 저장 키로 쓰면 일부 S3 호환 스토리지
    # (Supabase Storage 등)가 "Invalid key"로 거부한다. 확장자만 남기고
    # 나머지는 ASCII-safe한 랜덤 값으로 대체 — 버전 구분은 DB의 version
    # 필드가 담당하므로 파일명 자체가 원본을 보존할 필요는 없다. 사람이 보는
    # 파일명은 저장 키가 아니라 다운로드 시점에 display_filename()으로 따로 만든다.
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    return f'packaging/{instance.product_id}/{uuid.uuid4().hex}.{ext}'


class Product(models.Model):
    class Category(models.TextChoices):
        LABEL = 'LABEL', '병제품 부착 라벨'
        PP_BAG = 'PP_BAG', 'PP재질 포대 포장재'

    class ProductLine(models.TextChoices):
        FERTILIZER = 'FERTILIZER', '비료'
        CROP_PROTECTION = 'CROP_PROTECTION', '작물보호제'

    code = models.CharField(max_length=30, unique=True, verbose_name='품목코드',
                             help_text='품목명이 바뀌어도 유지되는 고유 식별자')
    name = models.CharField(max_length=120, verbose_name='품목명')
    category = models.CharField(max_length=20, choices=Category.choices)
    product_line = models.CharField(max_length=20, choices=ProductLine.choices)
    is_active = models.BooleanField(
        default=True, verbose_name='표시 여부',
        help_text='꺼두면 데이터는 유지한 채 품목 목록·검색·신규요청 선택지에서만 숨겨진다(관리자 화면의 "숨기기").',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = '품목'
        verbose_name_plural = '품목'

    def __str__(self):
        return f'[{self.code}] {self.name}'

    def current_final_file(self):
        return self.files.filter(
            status=PackagingFile.Status.FINAL_APPROVED, is_active=True,
        ).order_by('-version').first()

    def has_open_request(self):
        from workflow.models import ReorderRequest
        return self.requests.exclude(status__in=ReorderRequest.TERMINAL_STATUSES).order_by('-created_at').first()


class PackagingFile(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', '작업중'
        FINAL_APPROVED = 'FINAL_APPROVED', '최종 승인본'
        SUPERSEDED = 'SUPERSEDED', '이전 버전'

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='files')
    version = models.PositiveIntegerField(editable=False)
    ai_file = models.FileField(upload_to=packaging_upload_to)
    jpg_file = models.ImageField(upload_to=packaging_upload_to)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    note = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(
        default=True, verbose_name='표시 여부',
        help_text='꺼두면 데이터는 유지한 채 버전 이력·다운로드 목록에서만 숨겨진다(관리자 화면의 "숨기기").',
    )

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='uploaded_files')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_files')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['product', '-version']
        verbose_name = '포장지 파일'
        verbose_name_plural = '포장지 파일'
        constraints = [
            models.UniqueConstraint(fields=['product', 'version'], name='unique_product_version'),
        ]

    def __str__(self):
        return f'{self.product.name} v{self.version} ({self.get_status_display()})'

    def save(self, *args, **kwargs):
        if self.version is None:
            last = PackagingFile.objects.filter(product=self.product).order_by('-version').first()
            self.version = (last.version + 1) if last else 1
        super().save(*args, **kwargs)

    def approve(self, by_user):
        """최종 승인 처리 — 잠금 + 이전 승인본은 자동으로 이력(SUPERSEDED)으로 전환."""
        PackagingFile.objects.filter(
            product=self.product, status=PackagingFile.Status.FINAL_APPROVED
        ).exclude(pk=self.pk).update(status=PackagingFile.Status.SUPERSEDED)
        self.status = PackagingFile.Status.FINAL_APPROVED
        self.approved_by = by_user
        self.approved_at = timezone.now()
        self.save(update_fields=['status', 'approved_by', 'approved_at'])

    def within_exception_window(self, days=90):
        return bool(self.approved_at) and (timezone.now() - self.approved_at) <= timedelta(days=days)

    def is_locked(self):
        return self.status == PackagingFile.Status.FINAL_APPROVED

    def _display_filename(self, field_file):
        if not field_file:
            return ''
        ext = field_file.name.rsplit('.', 1)[-1] if '.' in field_file.name else 'bin'
        date_str = (self.approved_at or self.uploaded_at).strftime('%Y%m%d')
        name = sanitize_filename_part(self.product.name)
        # 다운로드 파일명 규칙: 품목명_버전_날짜.확장자
        return f'{name}_v{self.version}_{date_str}.{ext}'

    @property
    def ai_display_filename(self):
        return self._display_filename(self.ai_file)

    @property
    def jpg_display_filename(self):
        return self._display_filename(self.jpg_file)
