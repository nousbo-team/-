from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import PackagingFile, Product
from workflow.models import Notification, RequestEvent, ReorderRequest


class Command(BaseCommand):
    help = (
        '품목·파일·재발주 건·이력·알림을 전부 삭제하고 데모 데이터로 다시 채웁니다. '
        '계정(로그인 아이디)은 그대로 유지됩니다.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-reseed', action='store_true',
            help='삭제만 하고 seed_demo_data는 실행하지 않음',
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            Notification.objects.all().delete()
            RequestEvent.objects.all().delete()
            ReorderRequest.objects.all().delete()

            file_count = PackagingFile.objects.count()
            for pkg in PackagingFile.objects.all():
                if pkg.ai_file:
                    pkg.ai_file.delete(save=False)
                if pkg.jpg_file:
                    pkg.jpg_file.delete(save=False)
            PackagingFile.objects.all().delete()

            product_count = Product.objects.count()
            Product.objects.all().delete()

        self.stdout.write(self.style.SUCCESS(
            f'삭제 완료: 품목 {product_count}건, 파일 {file_count}건, 재발주 건/이력/알림 전체. '
            f'계정은 유지됩니다.'
        ))

        if not options['no_reseed']:
            call_command('seed_demo_data')
