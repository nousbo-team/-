import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def packaging_upload_to(instance, filename):
    # 원본 파일명(한글 등)을 그대로 저장 키로 쓰면 일부 S3 호환 스토리지
    # (Supabase Storage 등)가 "Invalid key"로 거부한다. 확장자만 남기고
    # 나머지는 ASCII-safe한 랜덤 값으로 대체 — 버전 구분은 DB의 version
    # 필드가 담당하므로 파일명 자체가 원본을 보존할 필요는 없다.
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = '품목'
        verbose_name_plural = '품목'

    def __str__(self):
        return f'[{self.code}] {self.name}'

    def current_final_file(self):
        return self.files.filter(status=PackagingFile.Status.FINAL_APPROVED).order_by('-version').first()

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
