import io

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase

from .models import PackagingFile, Product

User = get_user_model()


def _jpg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (10, 10), color=(1, 2, 3)).save(buf, format='JPEG')
    return buf.getvalue()


class SoftDeleteTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('u', password='x')
        self.product = Product.objects.create(
            code='SOFT-0001', name='숨김테스트', category=Product.Category.LABEL,
            product_line=Product.ProductLine.FERTILIZER)
        self.pkg = PackagingFile.objects.create(
            product=self.product,
            ai_file=ContentFile(b'ai', name='a.ai'),
            jpg_file=ContentFile(_jpg_bytes(), name='a.jpg'),
            uploaded_by=self.user,
        )
        self.pkg.approve(self.user)

    def test_hidden_product_excluded_from_active_queryset(self):
        self.assertIn(self.product, Product.objects.filter(is_active=True))
        self.product.is_active = False
        self.product.save(update_fields=['is_active'])
        self.assertNotIn(self.product, Product.objects.filter(is_active=True))
        # 데이터 자체는 그대로 남아있다.
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_hidden_file_excluded_from_current_final_file(self):
        self.assertEqual(self.product.current_final_file(), self.pkg)
        self.pkg.is_active = False
        self.pkg.save(update_fields=['is_active'])
        self.assertIsNone(self.product.current_final_file())
        self.assertTrue(PackagingFile.objects.filter(pk=self.pkg.pk).exists())
