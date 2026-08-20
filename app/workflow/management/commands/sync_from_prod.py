import os

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from accounts.models import UserProfile
from catalog.models import PackagingFile, Product
from workflow.models import Notification, RequestEvent, ReorderRequest

User = get_user_model()

# 자식 → 부모 순서(삭제할 때 PROTECT 제약을 지키기 위함). 적재할 때는 이 리스트를
# 뒤집어서 부모 → 자식 순서로 저장한다.
MODELS_CHILD_TO_PARENT = [Notification, RequestEvent, ReorderRequest, PackagingFile, Product, UserProfile, User]


class Command(BaseCommand):
    help = (
        '운영 서버(Supabase) DB를 읽어와 로컬 개발 DB(SQLite)를 운영과 동일한 상태로 맞춘다. '
        '로컬 데이터는 전부 지워지고 운영 데이터로 교체된다 — 운영 DB는 읽기만 하며 절대 쓰지 않는다. '
        '.env에 PROD_DATABASE_URL이 필요하고, 실제 AI/JPG 파일까지 받으려면 PROD_AWS_* 값도 필요하다.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--no-files', action='store_true',
                             help='DB 데이터만 받고 실제 AI/JPG 파일은 내려받지 않음(빠름)')

    def handle(self, *args, **options):
        if 'prod' not in connections.databases:
            raise CommandError(
                '.env에 PROD_DATABASE_URL이 없습니다. Render 대시보드 → nousbo-packaging → '
                'Environment 탭에서 DATABASE_URL 값을 복사해 app/.env에\n'
                '  PROD_DATABASE_URL=그 값\n'
                '으로 추가한 뒤 다시 실행하세요.')

        self.stdout.write('운영 DB에서 데이터를 읽는 중...')
        objects_by_model = {}
        total = 0
        for model in MODELS_CHILD_TO_PARENT:
            rows = list(model.objects.using('prod').all())
            objects_by_model[model] = rows
            total += len(rows)
            self.stdout.write(f'  {model._meta.verbose_name}: {len(rows)}건')

        self.stdout.write('로컬 데이터를 지우고 운영 데이터로 교체하는 중...')
        for model in MODELS_CHILD_TO_PARENT:  # 자식부터 삭제
            model.objects.using('default').all().delete()

        for model in reversed(MODELS_CHILD_TO_PARENT):  # 부모부터 저장
            for obj in objects_by_model[model]:
                obj.save(using='default', force_insert=True)

        self.stdout.write(self.style.SUCCESS(f'DB 동기화 완료: 총 {total}건.'))

        if options['no_files']:
            self.stdout.write('--no-files 옵션 — 실제 파일은 받지 않았습니다.')
            return
        self._sync_files()

    def _sync_files(self):
        bucket = os.environ.get('PROD_AWS_STORAGE_BUCKET_NAME')
        if not bucket:
            self.stdout.write(self.style.WARNING(
                'PROD_AWS_STORAGE_BUCKET_NAME 등이 .env에 없어 실제 파일(AI/JPG)은 받지 않았습니다. '
                '파일까지 받으려면 PROD_AWS_STORAGE_BUCKET_NAME/PROD_AWS_ACCESS_KEY_ID/'
                'PROD_AWS_SECRET_ACCESS_KEY/PROD_AWS_S3_ENDPOINT_URL을 .env에 추가하세요.'))
            return

        from botocore.config import Config as BotoConfig
        from storages.backends.s3 import S3Storage

        prod_storage = S3Storage(
            bucket_name=bucket,
            access_key=os.environ.get('PROD_AWS_ACCESS_KEY_ID', ''),
            secret_key=os.environ.get('PROD_AWS_SECRET_ACCESS_KEY', ''),
            endpoint_url=os.environ.get('PROD_AWS_S3_ENDPOINT_URL', ''),
            region_name=os.environ.get('PROD_AWS_S3_REGION_NAME', 'ap-northeast-2'),
            querystring_auth=True,
            config=BotoConfig(signature_version='s3v4', s3={'addressing_style': 'path'}),
        )

        pending = []
        for pkg in PackagingFile.objects.using('default').all():
            for field in (pkg.ai_file, pkg.jpg_file):
                if field and field.name:
                    pending.append(field.name)

        self.stdout.write(f'파일 {len(pending)}개 확인 중...')
        downloaded = skipped = failed = 0
        for name in pending:
            if default_storage.exists(name):
                skipped += 1
                continue
            try:
                with prod_storage.open(name, 'rb') as f:
                    default_storage.save(name, ContentFile(f.read()))
                downloaded += 1
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.WARNING(f'  다운로드 실패: {name} ({e})'))

        self.stdout.write(self.style.SUCCESS(
            f'파일 동기화 완료 — 새로 받음 {downloaded}건, 이미 있어 건너뜀 {skipped}건, 실패 {failed}건.'))
