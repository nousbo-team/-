import io

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from accounts.models import UserProfile
from catalog.models import PackagingFile, Product

from . import services, views_bulk
from .models import ReorderRequest

User = get_user_model()


def _jpg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (10, 10), color=(1, 2, 3)).save(buf, format='JPEG')
    return buf.getvalue()


class WorkflowTestCase(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user('req', password='x')
        self.reviewer = User.objects.create_user('rev', password='x')
        self.backup = User.objects.create_user('backup', password='x')
        self.designer = User.objects.create_user('des', password='x')
        self.approver = User.objects.create_user('app', password='x')

        UserProfile.objects.create(user=self.requester, role=UserProfile.Role.REQUESTER)
        self.reviewer_profile = UserProfile.objects.create(
            user=self.reviewer, role=UserProfile.Role.REVIEWER, backup_user=self.backup)
        UserProfile.objects.create(user=self.backup, role=UserProfile.Role.REVIEWER)
        UserProfile.objects.create(user=self.designer, role=UserProfile.Role.DESIGNER)
        UserProfile.objects.create(user=self.approver, role=UserProfile.Role.APPROVER)

        self.product = Product.objects.create(
            code='TEST-0001', name='테스트 라벨', category=Product.Category.LABEL,
            product_line=Product.ProductLine.FERTILIZER)
        self.file_v1 = PackagingFile.objects.create(
            product=self.product,
            ai_file=ContentFile(b'ai-bytes', name='t1.ai'),
            jpg_file=ContentFile(_jpg_bytes(), name='t1.jpg'),
            uploaded_by=self.designer,
        )
        self.file_v1.approve(self.approver)

    def _new_request(self):
        req, existing = services.create_request(self.product, self.requester, ReorderRequest.Reason.STOCK_SHORTAGE)
        self.assertIsNone(existing)
        return req

    def test_duplicate_request_detection(self):
        req = self._new_request()
        again, existing = services.create_request(self.product, self.requester, ReorderRequest.Reason.STOCK_SHORTAGE)
        self.assertIsNone(again)
        self.assertEqual(existing.pk, req.pk)

    def test_reject_requires_reason(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.FINAL_REVIEW)
        with self.assertRaises(services.ValidationErrorWF):
            services.final_decision(req, self.approver, 'REJECT', reason='')

    def test_reviewer_cannot_reject(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        with self.assertRaises(services.PermissionDeniedError):
            services.final_decision(req, self.reviewer, 'REJECT', reason='사유')

    def test_designer_only_can_upload(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정 필요')
        req.refresh_from_db()
        ai = ContentFile(b'ai2', name='t2.ai')
        jpg = ContentFile(_jpg_bytes(), name='t2.jpg')
        with self.assertRaises(services.PermissionDeniedError):
            services.design_upload(req, self.reviewer, ai, jpg)

    def test_reject_then_reapprove_locks_new_version(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정')
        req.refresh_from_db()
        ai = ContentFile(b'ai2', name='t2.ai')
        jpg = ContentFile(_jpg_bytes(), name='t2.jpg')
        services.design_upload(req, self.designer, ai, jpg)
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.REVIEW1)
        self.assertEqual(req.current_file.version, 2)

        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.approver, 'REJECT', reason='표시사항 오류')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.REVIEW1)

        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.approver, 'APPROVE')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.APPROVED)
        self.file_v1.refresh_from_db()
        self.assertEqual(self.file_v1.status, PackagingFile.Status.SUPERSEDED)
        req.current_file.refresh_from_db()
        self.assertEqual(req.current_file.status, PackagingFile.Status.FINAL_APPROVED)
        self.assertEqual(req.current_file.version, 2)

        services.handoff(req, self.reviewer)
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.COMPLETED)

    def test_exception_skip_final_review(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL', use_exception=True)
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.COMPLETED)
        self.assertTrue(req.used_exception)

    def test_exception_not_available_after_90_days(self):
        self.file_v1.approved_at = timezone.now() - timedelta(days=200)
        self.file_v1.save(update_fields=['approved_at'])
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL', use_exception=True)
        req.refresh_from_db()
        # use_exception requested but file outside window -> should go to FINAL_REVIEW, not skip
        self.assertEqual(req.status, ReorderRequest.Status.FINAL_REVIEW)
        self.assertFalse(req.used_exception)

    def test_backup_routing_only_active_when_away(self):
        self.assertEqual(services.effective_reviewers(), [self.reviewer])
        self.reviewer_profile.is_away = True
        self.reviewer_profile.save()
        reviewers = services.effective_reviewers()
        self.assertIn(self.reviewer, reviewers)
        self.assertIn(self.backup, reviewers)

    def test_approve_without_file_blocked(self):
        empty_product = Product.objects.create(
            code='TEST-0002', name='파일없는 제품', category=Product.Category.LABEL,
            product_line=Product.ProductLine.FERTILIZER)
        req, _ = services.create_request(empty_product, self.requester, ReorderRequest.Reason.NEEDS_REVISION)
        self.assertIsNone(req.current_file)
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='최초 제작')
        req.refresh_from_db()
        ai = ContentFile(b'ai', name='n1.ai')
        jpg = ContentFile(_jpg_bytes(), name='n1.jpg')
        services.design_upload(req, self.designer, ai, jpg)
        req.refresh_from_db()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.approver, 'APPROVE')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.APPROVED)

    def test_request_no_assigned_and_unique(self):
        req1 = self._new_request()
        self.assertTrue(req1.request_no.startswith('RQ-'))
        other_product = Product.objects.create(
            code='TEST-0004', name='다른 품목', category=Product.Category.LABEL,
            product_line=Product.ProductLine.FERTILIZER)
        req2, _ = services.create_request(other_product, self.requester, ReorderRequest.Reason.STOCK_SHORTAGE)
        self.assertNotEqual(req1.request_no, req2.request_no)
        self.assertTrue(req2.request_no.endswith('-002'))

    def test_bulk_resolve_product_upserts_by_code_and_renames(self):
        # 기존 품목코드로 다시 매핑되면 품목명이 바뀌어도 같은 품목으로 갱신되어야 한다.
        map_row = {'item_code': 'TEST-0001', 'product_name': '테스트 라벨(개명)', 'note': '', 'approver': '', 'approved_date': None}
        resolved = views_bulk._resolve_product('아무파일', map_row)
        self.assertEqual(resolved.pk, self.product.pk)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, '테스트 라벨(개명)')

    def test_bulk_resolve_product_creates_new_when_code_unknown(self):
        map_row = {'item_code': 'NEW-9999', 'product_name': '신규 품목', 'note': '', 'approver': '', 'approved_date': None}
        resolved = views_bulk._resolve_product('아무파일', map_row)
        self.assertEqual(resolved.code, 'NEW-9999')
        self.assertEqual(resolved.name, '신규 품목')

    def test_wrong_stage_action_rejected(self):
        req = self._new_request()
        with self.assertRaises(services.ValidationErrorWF):
            services.final_decision(req, self.approver, 'APPROVE')

    def test_approve_guard_when_no_file_attached(self):
        empty_product = Product.objects.create(
            code='TEST-0003', name='파일없는 제품2', category=Product.Category.LABEL,
            product_line=Product.ProductLine.FERTILIZER)
        req, _ = services.create_request(empty_product, self.requester, ReorderRequest.Reason.NEEDS_REVISION)
        self.assertIsNone(req.current_file)
        req.status = ReorderRequest.Status.FINAL_REVIEW
        req.save(update_fields=['status'])
        with self.assertRaises(services.ValidationErrorWF):
            services.final_decision(req, self.approver, 'APPROVE')
