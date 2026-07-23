import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import UserProfile
from catalog.models import PackagingFile, Product

DEMO_PASSWORD = '1234'
EMAIL_DOMAIN = 'nousbo.com'


def _make_jpg_bytes(color=(150, 180, 150)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (200, 200), color=color).save(buf, format='JPEG')
    return buf.getvalue()


def _make_ai_bytes(label):
    return f'%PDF-1.5-ILLUSTRATOR-PLACEHOLDER%\n(demo AI file for {label})'.encode('utf-8')


class Command(BaseCommand):
    help = '데모용 계정(실제 인물 기준 5명 + 관리자) 및 샘플 제품/파일을 생성합니다.'

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()

        def make_user(username, first_name, is_staff=False, is_superuser=False):
            email = f'{username}@{EMAIL_DOMAIN}'
            user, created = User.objects.get_or_create(
                username=username,
                defaults={'first_name': first_name, 'email': email, 'is_staff': is_staff, 'is_superuser': is_superuser},
            )
            user.set_password(DEMO_PASSWORD)
            user.first_name = first_name
            user.email = email
            user.is_staff = is_staff
            user.is_superuser = is_superuser
            user.save()
            return user

        admin = make_user('nousbo', '관리자', is_staff=True, is_superuser=True)

        haon = make_user('haon', '이정인')
        isis9 = make_user('isis9', '박현경')
        shindeok_kim = make_user('shindeok_kim', '김신덕')
        guychj = make_user('guychj', '최효진')
        hjcho = make_user('hjcho', '조현종')

        UserProfile.objects.update_or_create(
            user=haon, defaults=dict(role=UserProfile.Role.REQUESTER, department='울산공장', title='매니저'))
        UserProfile.objects.update_or_create(
            user=shindeok_kim, defaults=dict(role=UserProfile.Role.REVIEWER, department='브랜드기획팀', title='본부장'))
        isis9_profile, _ = UserProfile.objects.update_or_create(
            user=isis9, defaults=dict(role=UserProfile.Role.REVIEWER, department='브랜드기획팀', title='팀장',
                                       backup_user=shindeok_kim))
        UserProfile.objects.update_or_create(
            user=guychj, defaults=dict(role=UserProfile.Role.DESIGNER, department='디자인', title='차장'))
        UserProfile.objects.update_or_create(
            user=hjcho, defaults=dict(role=UserProfile.Role.APPROVER, department='연구소', title='소장'))

        self.stdout.write(self.style.SUCCESS(
            f'계정 생성 완료 (비밀번호: {DEMO_PASSWORD}): haon, isis9, shindeok_kim, guychj, hjcho, nousbo'))

        samples = [
            dict(code='FERT-PP-1001', name='그린비료 20kg PP포대', category=Product.Category.PP_BAG,
                 product_line=Product.ProductLine.FERTILIZER, days_ago=30),
            dict(code='CROP-LB-2001', name='세이프가드 작물보호제 병 라벨', category=Product.Category.LABEL,
                 product_line=Product.ProductLine.CROP_PROTECTION, days_ago=200),
            dict(code='FERT-LB-1002', name='골드복합비료 라벨', category=Product.Category.LABEL,
                 product_line=Product.ProductLine.FERTILIZER, days_ago=None),
        ]

        for s in samples:
            product, created = Product.objects.get_or_create(
                code=s['code'], defaults=dict(name=s['name'], category=s['category'], product_line=s['product_line']))
            if not created or s['days_ago'] is None:
                continue
            pkg = PackagingFile.objects.create(
                product=product,
                ai_file=ContentFile(_make_ai_bytes(product.name), name=f'{product.name}_v1.ai'),
                jpg_file=ContentFile(_make_jpg_bytes(), name=f'{product.name}_v1.jpg'),
                uploaded_by=guychj,
                note='초기 등록(데모 데이터)',
            )
            pkg.approve(hjcho)
            pkg.approved_at = timezone.now() - timedelta(days=s['days_ago'])
            pkg.save(update_fields=['approved_at'])

        self.stdout.write(self.style.SUCCESS('샘플 제품 3건 생성 완료 (2건은 최종 승인본 포함, 1건은 미등록 상태).'))
        self.stdout.write(self.style.SUCCESS(f'관리자 계정: nousbo / {DEMO_PASSWORD} (/admin/)'))
