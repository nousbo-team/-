import io
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from accounts.models import UserProfile
from catalog.models import PackagingFile, Product

from . import assistant, services
from .models import ReorderRequest, RequestEvent

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

    def test_reviewer_can_reject(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.reviewer, 'REJECT', reason='표시사항 오류')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.REVIEW1)

    def test_reviewer_cannot_approve_or_request_revision(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        with self.assertRaises(services.PermissionDeniedError):
            services.final_decision(req, self.reviewer, 'APPROVE')
        with self.assertRaises(services.PermissionDeniedError):
            services.final_decision(req, self.reviewer, 'REVISION', reason='수정')

    def test_designer_cannot_reject(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        with self.assertRaises(services.PermissionDeniedError):
            services.final_decision(req, self.designer, 'REJECT', reason='사유')

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

    def test_reviewer_can_complete_without_final_review(self):
        """디자인 확정 후 연구소 검수를 건너뛰고 바로 완료 — 파일도 최종 승인본이 되어야 한다."""
        req = self._new_request()
        services.complete_without_final_review(req, self.reviewer, reason='기존 승인본과 동일한 규격이라 검수 불필요')
        req.refresh_from_db()

        self.assertEqual(req.status, ReorderRequest.Status.COMPLETED)
        self.assertTrue(req.used_exception)
        req.current_file.refresh_from_db()
        self.assertEqual(req.current_file.status, PackagingFile.Status.FINAL_APPROVED)
        # 완료된 건의 파일이 품목의 현재 최종본으로 잡혀야 한다.
        self.assertEqual(self.product.current_final_file(), req.current_file)

        # 검수를 건너뛴 사실과 사유가 이력에 남아야 한다.
        event = req.events.filter(action=RequestEvent.Action.REVIEW_DIRECT_COMPLETE).first()
        self.assertIsNotNone(event)
        self.assertIn('검수 불필요', event.note)

    def test_direct_complete_requires_reason(self):
        req = self._new_request()
        with self.assertRaises(services.ValidationErrorWF):
            services.complete_without_final_review(req, self.reviewer, reason='   ')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.REVIEW1)

    def test_direct_complete_denied_for_non_reviewer(self):
        """연구소·디자인·요청자는 이 경로를 쓸 수 없다 — 창구 담당자 전용."""
        req = self._new_request()
        for actor in (self.approver, self.designer, self.requester):
            with self.subTest(actor=actor.username):
                with self.assertRaises(services.PermissionDeniedError):
                    services.complete_without_final_review(req, actor, reason='사유')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.REVIEW1)

    def test_direct_complete_notifies_requester_and_lab(self):
        req = self._new_request()
        services.complete_without_final_review(req, self.reviewer, reason='검수 생략 사유')
        notified = set(req.notifications.values_list('user', flat=True))
        self.assertIn(self.requester.pk, notified)
        # 자기 검수를 건너뛴 것이므로 연구소도 통보받아야 한다.
        self.assertIn(self.approver.pk, notified)

    def test_direct_complete_only_from_review1(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.FINAL_REVIEW)
        with self.assertRaises(services.ValidationErrorWF):
            services.complete_without_final_review(req, self.reviewer, reason='사유')

    def test_exception_skip_marks_file_approved(self):
        """3개월 예외로 완료해도 파일이 최종 승인본으로 등록돼야 한다(예전엔 누락됐음)."""
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL', use_exception=True)
        req.refresh_from_db()
        req.current_file.refresh_from_db()
        self.assertEqual(req.current_file.status, PackagingFile.Status.FINAL_APPROVED)
        self.assertEqual(self.product.current_final_file(), req.current_file)

    def test_requester_can_attach_reference_file_when_creating(self):
        """울산공장이 요청 등록 시 올린 자료가 이력에 남고, 원본 파일명이 보존돼야 한다."""
        upload = SimpleUploadedFile('표시사항 변경안.pdf', b'%PDF-1.4 fake', content_type='application/pdf')
        req, existing = services.create_request(
            self.product, self.requester, ReorderRequest.Reason.NEEDS_REVISION,
            detail='표시사항 문구 변경', attachment=upload)
        self.assertIsNone(existing)

        event = req.events.filter(action=RequestEvent.Action.SUBMITTED).first()
        self.assertTrue(event.attachment)
        self.assertEqual(event.attachment_original_name, '표시사항 변경안.pdf')
        self.assertIn('요청사항 참고 파일 첨부됨', event.note)
        # 다운로드 파일명은 요청번호 기준 규칙을 따른다.
        self.assertTrue(event.attachment_filename.endswith('.pdf'))
        self.assertIn(req.request_no, event.attachment_filename)

    def test_create_request_without_attachment_still_works(self):
        req, _ = services.create_request(
            self.product, self.requester, ReorderRequest.Reason.STOCK_SHORTAGE, detail='첨부 없음')
        event = req.events.filter(action=RequestEvent.Action.SUBMITTED).first()
        self.assertFalse(event.attachment)
        self.assertNotIn('참고 파일 첨부됨', event.note)

    def test_approver_can_attach_file_when_rejecting(self):
        """연구소가 반려하며 올린 수정사항 자료가 이력에 남아 다른 담당자에게 공유돼야 한다."""
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()

        upload = SimpleUploadedFile('수정사항 정리.xlsx', b'xlsx-bytes')
        services.final_decision(req, self.approver, 'REJECT', reason='표시사항 규정 위반', attachment=upload)
        req.refresh_from_db()

        self.assertEqual(req.status, ReorderRequest.Status.REVIEW1)
        event = req.events.filter(action=RequestEvent.Action.FINAL_REJECT).first()
        self.assertTrue(event.attachment)
        self.assertEqual(event.attachment_original_name, '수정사항 정리.xlsx')

    def test_reject_attachment_is_downloadable_by_another_role(self):
        """첨부의 목적은 공유다 — 올린 사람이 아닌 담당자도 받을 수 있어야 한다."""
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.approver, 'REJECT', reason='사유',
                                attachment=SimpleUploadedFile('공유.txt', b'shared'))
        event = req.events.filter(action=RequestEvent.Action.FINAL_REJECT).first()

        self.client.force_login(self.reviewer)
        resp = self.client.get(f'/attachments/{event.pk}/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp['Content-Disposition'])

    def test_revision_can_also_carry_attachment(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.approver, 'REVISION', reason='경미 보완',
                                attachment=SimpleUploadedFile('보완.txt', b'x'))
        event = req.events.filter(action=RequestEvent.Action.FINAL_REVISION).first()
        self.assertTrue(event.attachment)

    def _flow_state(self, req, label):
        return next(s['state'] for s in req.flow_progress()['steps'] if s['label'] == label)

    def test_flow_breaks_out_intake_review_and_design_steps(self):
        """예전엔 '1차검토' 하나에 뭉뚱그려져 있던 구간이 실제 단계로 쪼개져야 한다."""
        req = self._new_request()
        labels = [s['label'] for s in req.flow_progress()['steps']]
        self.assertEqual(labels, ['요청접수', '내부확인', '디자인수정', '최종검수', '전달대기', '완료'])
        self.assertNotIn('1차검토', labels)

        self.assertEqual(self._flow_state(req, '요청접수'), 'done')
        self.assertEqual(self._flow_state(req, '내부확인'), 'current')

    def test_flow_reports_who_must_act_now(self):
        """단계마다 지금 누구 차례인지 함께 나와야 화면에서 안내할 수 있다."""
        req = self._new_request()
        self.assertEqual(req.flow_progress()['current_owner'], '브랜드기획팀')

        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정')
        req.refresh_from_db()
        self.assertEqual(req.flow_progress()['current_owner'], '디자인팀')
        self.assertEqual(self._flow_state(req, '디자인수정'), 'current')

        ai = ContentFile(b'ai2', name='t2.ai')
        jpg = ContentFile(_jpg_bytes(), name='t2.jpg')
        services.design_upload(req, self.designer, ai, jpg)
        req.refresh_from_db()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        self.assertEqual(req.flow_progress()['current_owner'], '연구소')

        services.final_decision(req, self.approver, 'APPROVE')
        req.refresh_from_db()
        self.assertEqual(req.flow_progress()['current_owner'], '브랜드기획팀')

    def test_design_step_marked_done_only_when_actually_used(self):
        """디자인수정은 모든 건이 거치는 단계가 아니다 — 거치지 않고 지나간 건을
        '완료'로 칠하면 이력과 어긋난다."""
        skipped = self._new_request()
        services.review_decision(skipped, self.reviewer, 'CONFIRM_FINAL')
        skipped.refresh_from_db()
        self.assertEqual(self._flow_state(skipped, '디자인수정'), 'skipped')

        services.final_decision(skipped, self.approver, 'APPROVE')
        skipped.refresh_from_db()
        services.handoff(skipped, self.reviewer)
        skipped.refresh_from_db()
        # 완료된 뒤에도 거치지 않은 단계는 계속 구분돼야 한다.
        self.assertEqual(self._flow_state(skipped, '디자인수정'), 'skipped')
        self.assertEqual(self._flow_state(skipped, '최종검수'), 'done')

    def test_design_step_done_when_it_actually_happened(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정')
        req.refresh_from_db()
        services.design_upload(req, self.designer, ContentFile(b'a', name='x.ai'),
                               ContentFile(_jpg_bytes(), name='x.jpg'))
        req.refresh_from_db()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        self.assertEqual(self._flow_state(req, '디자인수정'), 'done')

    def test_cancelled_request_marks_every_step_cancelled(self):
        req = self._new_request()
        services.cancel_request(req, self.requester, reason='단종')
        req.refresh_from_db()
        flow = req.flow_progress()
        self.assertTrue(flow['cancelled'])
        self.assertIsNone(flow['current_owner'])
        self.assertTrue(all(s['state'] == 'cancelled' for s in flow['steps']))

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

    def test_requester_can_cancel_own_request(self):
        req = self._new_request()
        services.cancel_request(req, self.requester, reason='재고 확보됨')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.CANCELLED)

    def test_requester_cannot_cancel_after_completion(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL', use_exception=True)
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.COMPLETED)
        with self.assertRaises(services.ValidationErrorWF):
            services.cancel_request(req, self.requester, reason='늦은 취소')

    def test_cancel_requires_reason(self):
        req = self._new_request()
        with self.assertRaises(services.ValidationErrorWF):
            services.cancel_request(req, self.requester, reason='')

    def test_reviewer_can_cancel_at_review1(self):
        req = self._new_request()
        services.cancel_request(req, self.reviewer, reason='중복 요청')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.CANCELLED)

    def test_reviewer_cannot_cancel_at_design_edit(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정')
        req.refresh_from_db()
        with self.assertRaises(services.PermissionDeniedError):
            services.cancel_request(req, self.reviewer, reason='임의 취소 시도')

    def test_other_role_cannot_cancel(self):
        req = self._new_request()
        with self.assertRaises(services.PermissionDeniedError):
            services.cancel_request(req, self.designer, reason='권한 없음')

    def test_designer_reject_reverts_to_review1(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.DESIGN_EDIT)
        services.design_reject(req, self.designer, reason='요청 내용 불명확')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.REVIEW1)

    def test_design_reject_requires_reason(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정')
        req.refresh_from_db()
        with self.assertRaises(services.ValidationErrorWF):
            services.design_reject(req, self.designer, reason='')

    def test_design_reject_wrong_stage(self):
        req = self._new_request()
        with self.assertRaises(services.ValidationErrorWF):
            services.design_reject(req, self.designer, reason='사유')

    def test_reviewer_can_revert_approval_to_final_review(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.approver, 'APPROVE')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.APPROVED)
        services.revert_approval(req, self.reviewer, reason='라벨 재검토 필요')
        req.refresh_from_db()
        self.assertEqual(req.status, ReorderRequest.Status.FINAL_REVIEW)

    def test_revert_approval_wrong_stage(self):
        req = self._new_request()
        with self.assertRaises(services.ValidationErrorWF):
            services.revert_approval(req, self.reviewer, reason='사유')

    def test_revert_approval_permission(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'CONFIRM_FINAL')
        req.refresh_from_db()
        services.final_decision(req, self.approver, 'APPROVE')
        req.refresh_from_db()
        with self.assertRaises(services.PermissionDeniedError):
            services.revert_approval(req, self.approver, reason='사유')

    def test_cancel_notifies_history_participants(self):
        req = self._new_request()
        services.review_decision(req, self.reviewer, 'NEEDS_EDIT', note='수정')
        req.refresh_from_db()
        ai = ContentFile(b'ai2', name='t2.ai')
        jpg = ContentFile(_jpg_bytes(), name='t2.jpg')
        services.design_upload(req, self.designer, ai, jpg)
        req.refresh_from_db()
        services.cancel_request(req, self.requester, reason='취소 사유')
        notified_users = set(req.notifications.values_list('user', flat=True))
        self.assertIn(self.requester.pk, notified_users)
        self.assertIn(self.reviewer.pk, notified_users)
        self.assertIn(self.designer.pk, notified_users)


class NoProfileAccountTestCase(TestCase):
    """nousbo 같은 프로필 없는(super)유저가 일반 화면에 들어와도 500이 나면 안 된다."""

    def setUp(self):
        self.admin = User.objects.create_user('admin_no_profile', password='x', is_staff=True, is_superuser=True)
        self.product = Product.objects.create(
            code='NP-0001', name='프로필없음 테스트', category=Product.Category.LABEL,
            product_line=Product.ProductLine.FERTILIZER)
        self.client.login(username='admin_no_profile', password='x')

    def test_dashboard_shows_no_profile_page_instead_of_crashing(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'workflow/no_profile.html')

    def test_new_request_redirects_instead_of_crashing(self):
        resp = self.client.get('/requests/new/', follow=True)
        self.assertEqual(resp.status_code, 200)

    def test_request_detail_renders_instead_of_crashing(self):
        req = ReorderRequest.objects.create(
            request_no='RQ-TEST-NP-001', product=self.product, requester=self.admin,
            reason=ReorderRequest.Reason.STOCK_SHORTAGE, status=ReorderRequest.Status.REVIEW1)
        resp = self.client.get(f'/requests/{req.pk}/')
        self.assertEqual(resp.status_code, 200)


class AssistantTestCase(TestCase):
    """AI 비서(assistant.py) — 실제 Gemini API를 호출하지 않고 requests.post를
    모의(mock) 처리해 검증한다(요금이 드는 외부 호출은 테스트에서 절대 하지 않음)."""

    def setUp(self):
        self.user = User.objects.create_user('assistant_tester', password='x')
        UserProfile.objects.create(user=self.user, role=UserProfile.Role.REQUESTER)
        self.product = Product.objects.create(
            code='AS-0001', name='비서테스트 품목', category=Product.Category.LABEL,
            product_line=Product.ProductLine.FERTILIZER)
        self.client.login(username='assistant_tester', password='x')

    def test_assistant_page_shows_disabled_state_without_api_key(self):
        with self.settings(GEMINI_API_KEY=''):
            resp = self.client.get('/assistant/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['assistant_disabled'])

    def test_ask_without_api_key_raises(self):
        with self.settings(GEMINI_API_KEY=''):
            with self.assertRaises(assistant.AssistantError):
                assistant.ask(self.user, '테스트 질문')

    def test_ask_view_returns_answer_from_mocked_gemini(self):
        req = ReorderRequest.objects.create(
            request_no='RQ-TEST-AS-001', product=self.product, requester=self.user,
            reason=ReorderRequest.Reason.STOCK_SHORTAGE, status=ReorderRequest.Status.REVIEW1)

        class _FakeResponse:
            status_code = 200

            def json(self):
                return {'candidates': [{'content': {'parts': [{'text': f'{req.request_no} 건이 검토중입니다.'}]}}]}

        with self.settings(GEMINI_API_KEY='fake-key-for-test'):
            with mock.patch('workflow.assistant.requests.post', return_value=_FakeResponse()) as post:
                resp = self.client.post(
                    '/assistant/ask/', data=json.dumps({'question': '내 발주 상태 알려줘'}),
                    content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload['ok'])
        self.assertIn(req.request_no, payload['answer'])
        # 컨텍스트에 이 요청번호가 실제로 담겨 Gemini에 전달됐는지도 확인.
        sent_payload = post.call_args.kwargs['json']
        sent_text = sent_payload['contents'][-1]['parts'][0]['text']
        self.assertIn(req.request_no, sent_text)

    def test_ask_view_rejects_empty_question(self):
        with self.settings(GEMINI_API_KEY='fake-key-for-test'):
            resp = self.client.post(
                '/assistant/ask/', data=json.dumps({'question': '  '}),
                content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_stream_view_emits_sse_chunks_from_mocked_gemini(self):
        """스트리밍 경로 — Gemini의 SSE 조각들이 그대로 화면용 이벤트로 흘러가는지.

        iter_lines는 실제 requests처럼 bytes를 돌려준다 — 예전엔 이 가짜가 이미 디코드된
        str을 주는 바람에, requests가 text/event-stream을 ISO-8859-1로 잘못 디코드해
        한글이 깨지던 버그를 테스트가 잡지 못했다."""
        def _sse(text):
            body = {'candidates': [{'content': {'parts': [{'text': text}]}}]}
            return ('data: ' + json.dumps(body, ensure_ascii=False)).encode('utf-8')

        class _FakeStreamResponse:
            status_code = 200

            def iter_lines(self, decode_unicode=False):
                lines = [_sse('안녕하'), b'', _sse('세요.')]
                if decode_unicode:
                    # requests는 charset 없는 text/* 응답을 ISO-8859-1로 가정한다 —
                    # 실제로 한글이 깨지던 그 동작을 그대로 흉내 내, 이 경로로 돌아가면
                    # 테스트가 깨진 글자를 보고 실패하게 만든다.
                    return iter([b.decode('iso-8859-1') for b in lines])
                return iter(lines)

            def close(self):
                pass

        with self.settings(GEMINI_API_KEY='fake-key-for-test'):
            with mock.patch('workflow.assistant.requests.post', return_value=_FakeStreamResponse()):
                resp = self.client.post(
                    '/assistant/ask/stream/', data=json.dumps({'question': '상태 알려줘'}),
                    content_type='application/json')
                body = b''.join(resp.streaming_content).decode()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/event-stream')
        self.assertIn('안녕하', body)
        self.assertIn('세요.', body)
        self.assertIn('"done": true', body)

    def test_stream_view_reports_error_inside_stream(self):
        """응답이 이미 시작된 뒤에는 상태코드를 못 바꾸므로, 오류도 스트림 안에서 나와야 한다."""
        class _FakeErrorResponse:
            status_code = 500
            text = 'boom'

            def json(self):
                return {'error': {'code': 500, 'status': 'INTERNAL', 'message': 'boom'}}

            def close(self):
                pass

        with self.settings(GEMINI_API_KEY='fake-key-for-test'):
            with mock.patch('workflow.assistant._RETRY_DELAY_SECONDS', 0):
                with mock.patch('workflow.assistant.requests.post', return_value=_FakeErrorResponse()):
                    resp = self.client.post(
                        '/assistant/ask/stream/', data=json.dumps({'question': '상태 알려줘'}),
                        content_type='application/json')
                    body = b''.join(resp.streaming_content).decode()

        self.assertEqual(resp.status_code, 200)
        self.assertIn('error', body)

    def test_error_messages_name_the_actual_cause(self):
        """실패 원인별로 다른 안내가 나와야 한다 — 전부 "응답하지 못했습니다"로 뭉뚱그리면
        사용량 초과인지 설정 문제인지 아무도 구분할 수 없다."""
        cases = [
            (429, 'RESOURCE_EXHAUSTED', '사용량'),
            (403, 'PERMISSION_DENIED', 'API 키'),
            (404, 'NOT_FOUND', '모델'),
        ]
        for code, status, expected in cases:
            class _FakeResponse:
                status_code = code
                text = status

                def json(self):
                    return {'error': {'code': code, 'status': status, 'message': 'nope'}}

                def close(self):
                    pass

            with self.subTest(code=code):
                with self.settings(GEMINI_API_KEY='fake-key-for-test'):
                    with mock.patch('workflow.assistant.requests.post', return_value=_FakeResponse()):
                        with self.assertRaises(assistant.AssistantError) as cm:
                            list(assistant.ask_stream(self.user, '질문'))
                self.assertIn(expected, str(cm.exception))

    def test_transient_server_error_is_retried_once(self):
        """구글 쪽 일시 장애(503)는 사용자에게 오류를 보이기 전에 조용히 한 번 더 시도한다."""
        class _Fake503:
            status_code = 503
            text = 'unavailable'

            def json(self):
                return {'error': {'code': 503, 'status': 'UNAVAILABLE', 'message': 'try again'}}

            def close(self):
                pass

        class _FakeOk:
            status_code = 200

            def iter_lines(self, decode_unicode=False):
                body = {'candidates': [{'content': {'parts': [{'text': '재시도 성공'}]}}]}
                return iter([('data: ' + json.dumps(body, ensure_ascii=False)).encode('utf-8')])

            def close(self):
                pass

        with self.settings(GEMINI_API_KEY='fake-key-for-test'):
            with mock.patch('workflow.assistant._RETRY_DELAY_SECONDS', 0):
                with mock.patch('workflow.assistant.requests.post',
                                side_effect=[_Fake503(), _FakeOk()]) as post:
                    chunks = list(assistant.ask_stream(self.user, '질문'))

        self.assertEqual(chunks, ['재시도 성공'])
        self.assertEqual(post.call_count, 2)

    def test_diagnostics_page_is_superuser_only(self):
        resp = self.client.get('/assistant/diagnostics/')
        self.assertEqual(resp.status_code, 403)

    def test_diagnostics_page_lists_models_for_superuser(self):
        admin = User.objects.create_user('assistant_admin', password='x', is_superuser=True, is_staff=True)
        self.client.force_login(admin)

        class _FakeModelsResponse:
            status_code = 200

            def json(self):
                return {'models': [
                    {'name': 'models/gemini-3.7-flash', 'supportedGenerationMethods': ['generateContent']},
                    {'name': 'models/embedding-001', 'supportedGenerationMethods': ['embedContent']},
                ]}

        with self.settings(GEMINI_API_KEY='fake-key-for-test', GEMINI_MODEL='gemini-3.7-flash'):
            with mock.patch('workflow.assistant.requests.get', return_value=_FakeModelsResponse()):
                resp = self.client.get('/assistant/diagnostics/')

        self.assertEqual(resp.status_code, 200)
        # generateContent를 지원하는 모델만 목록에 남아야 한다.
        self.assertEqual(resp.context['models'], ['gemini-3.7-flash'])
        self.assertTrue(resp.context['model_ok'])

    def test_stream_retries_without_thinking_config_on_400(self):
        """thinkingConfig를 모르는 모델이 400을 주면, 그 옵션만 빼고 한 번 더 시도한다."""
        class _Fake400:
            status_code = 400
            text = 'unknown field thinkingConfig'

            def close(self):
                pass

        class _FakeOk:
            status_code = 200

            def iter_lines(self, decode_unicode=False):
                body = {'candidates': [{'content': {'parts': [{'text': '재시도 성공'}]}}]}
                return iter([('data: ' + json.dumps(body, ensure_ascii=False)).encode('utf-8')])

            def close(self):
                pass

        with self.settings(GEMINI_API_KEY='fake-key-for-test'):
            with mock.patch('workflow.assistant.requests.post',
                            side_effect=[_Fake400(), _FakeOk()]) as post:
                chunks = list(assistant.ask_stream(self.user, '상태 알려줘'))

        self.assertEqual(chunks, ['재시도 성공'])
        self.assertEqual(post.call_count, 2)
        # 두 번째 호출에는 thinkingConfig가 빠져 있어야 한다.
        second_config = post.call_args_list[1].kwargs['json']['generationConfig']
        self.assertNotIn('thinkingConfig', second_config)
