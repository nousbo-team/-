"""
승인 워크플로우 상태 전이를 담당하는 서비스 레이어(P0-4). 뷰는 이 함수들만 호출하고
직접 status를 바꾸지 않는다 — 권한 검사·이력 기록·알림 발송을 한 곳에서 보장하기 위함.
"""
from django.db import transaction
from django.utils import timezone

from accounts.models import UserProfile
from catalog.models import PackagingFile

from .models import Notification, RequestEvent, ReorderRequest
from .notify import is_real_email_enabled, send_email_mock, send_kakao_mock


class WorkflowError(Exception):
    pass


class PermissionDeniedError(WorkflowError):
    pass


class ValidationErrorWF(WorkflowError):
    pass


def _role_users(role):
    return [p.user for p in UserProfile.objects.filter(role=role).select_related('user')]


def effective_reviewers():
    """현재 리뷰어 대기건을 처리할 수 있는 사용자 목록.

    박현경 팀장처럼 다른 누구의 backup_user도 아닌 리뷰어는 항상 포함된다.
    김신덕 본부장처럼 누군가의 backup_user로 지정된 리뷰어는, 그 담당자가
    부재중(is_away)일 때만 포함된다 — REVIEWER 역할을 갖고 있다는 사실만으로
    상시 노출되지 않는다(P0-8).
    """
    reviewer_profiles = list(
        UserProfile.objects.filter(role=UserProfile.Role.REVIEWER).select_related('user', 'backup_user'))
    backup_holders = {p.backup_user_id for p in reviewer_profiles if p.backup_user_id}

    active = []
    for profile in reviewer_profiles:
        if profile.user_id in backup_holders:
            primary_is_away = any(
                p.is_away for p in reviewer_profiles if p.backup_user_id == profile.user_id)
            if primary_is_away:
                active.append(profile.user)
        else:
            active.append(profile.user)
    return active


def effective_designers():
    return _role_users(UserProfile.Role.DESIGNER)


def effective_approvers():
    return _role_users(UserProfile.Role.APPROVER)


def _log(req, actor, action, note=''):
    return RequestEvent.objects.create(request=req, actor=actor, action=action, note=note, channel=RequestEvent.Channel.SYSTEM)


def _generate_request_no():
    """RQ-YYYYMMDD-### 형식의 요청번호를 당일 순번으로 채번한다(Task ID 개념)."""
    prefix = f'RQ-{timezone.localdate().strftime("%Y%m%d")}-'
    count_today = ReorderRequest.objects.select_for_update().filter(request_no__startswith=prefix).count()
    return f'{prefix}{count_today + 1:03d}'


def _notify(req, users, message, kakao=False):
    for u in users:
        Notification.objects.create(user=u, request=req, message=message)
    send_email_mock(users, f'[{req.request_no}] {req.product.name}', message)
    email_label = '이메일 발송' if is_real_email_enabled() else '이메일(모의) 발송'
    RequestEvent.objects.create(
        request=req, actor=None, action=RequestEvent.Action.NOTIFY,
        note=f'{email_label}: {message}', channel=RequestEvent.Channel.EMAIL_MOCK,
    )
    if kakao:
        send_kakao_mock(users, message)
        RequestEvent.objects.create(
            request=req, actor=None, action=RequestEvent.Action.NOTIFY,
            note=f'카카오톡/문자(모의) 발송: {message}', channel=RequestEvent.Channel.KAKAO_MOCK,
        )


def create_request(product, requester, reason):
    """재발주 요청 등록(P0-3). 동일 제품에 진행중인 건이 있으면 (None, 기존건)을 반환."""
    existing = product.has_open_request()
    if existing:
        return None, existing

    with transaction.atomic():
        req = ReorderRequest.objects.create(
            request_no=_generate_request_no(),
            product=product, requester=requester, reason=reason,
            status=ReorderRequest.Status.REVIEW1,
            current_file=product.current_final_file(),
        )
        _log(req, requester, RequestEvent.Action.SUBMITTED,
             note=f'재발주 요청 등록 ({req.get_reason_display()}) · 최종본 확인 요청')
        _notify(req, effective_reviewers(),
                f'"{product.name}" 재발주 요청이 등록되었습니다. 최종본 확인이 필요합니다.', kakao=True)
    return req, None


def review_decision(req, actor, decision, note='', use_exception=False):
    """1차검토(REVIEW1) 처리. decision: 'CONFIRM_FINAL' | 'NEEDS_EDIT'."""
    if actor not in effective_reviewers():
        raise PermissionDeniedError('1차 검토·관리 창구 담당자만 처리할 수 있습니다.')
    if req.status != ReorderRequest.Status.REVIEW1:
        raise ValidationErrorWF('현재 1차검토 단계가 아닙니다.')

    with transaction.atomic():
        if decision == 'NEEDS_EDIT':
            req.status = ReorderRequest.Status.DESIGN_EDIT
            req.save(update_fields=['status', 'updated_at'])
            _log(req, actor, RequestEvent.Action.REVIEW_REQUEST_EDIT, note=note)
            _notify(req, effective_designers(),
                    f'"{req.product.name}" 디자인 수정이 필요합니다: {note}', kakao=True)
        elif decision == 'CONFIRM_FINAL':
            if use_exception and req.current_file and req.current_file.within_exception_window():
                req.status = ReorderRequest.Status.COMPLETED
                req.used_exception = True
                req.save(update_fields=['status', 'used_exception', 'updated_at'])
                _log(req, actor, RequestEvent.Action.EXCEPTION_SKIP,
                     note='최근 3개월 이내 승인 이력이 있어 최종검수를 생략하고 완료 처리')
                _notify(req, [req.requester], f'"{req.product.name}" 재발주 건이 완료되었습니다(최종검수 생략).')
            else:
                req.status = ReorderRequest.Status.FINAL_REVIEW
                req.save(update_fields=['status', 'updated_at'])
                _log(req, actor, RequestEvent.Action.REVIEW_TO_FINAL, note=note)
                _notify(req, effective_approvers(),
                        f'"{req.product.name}" 최종 검수가 필요합니다.', kakao=True)
        else:
            raise ValidationErrorWF('알 수 없는 처리입니다.')
    return req


def design_upload(req, actor, ai_file, jpg_file, note=''):
    """디자인파일 수정 업로드. 새 버전 생성 후 1차검토로 재진입."""
    profile = getattr(actor, 'profile', None)
    if not profile or profile.role != UserProfile.Role.DESIGNER:
        raise PermissionDeniedError('디자인 담당자만 처리할 수 있습니다.')
    if req.status != ReorderRequest.Status.DESIGN_EDIT:
        raise ValidationErrorWF('현재 디자인 수정 단계가 아닙니다.')

    with transaction.atomic():
        new_file = PackagingFile.objects.create(
            product=req.product, ai_file=ai_file, jpg_file=jpg_file,
            uploaded_by=actor, note=note,
        )
        req.current_file = new_file
        req.status = ReorderRequest.Status.REVIEW1
        req.save(update_fields=['current_file', 'status', 'updated_at'])
        _log(req, actor, RequestEvent.Action.DESIGN_UPLOADED, note=f'v{new_file.version} 업로드: {note}')
        _notify(req, effective_reviewers(), f'"{req.product.name}" 수정본 재확인이 필요합니다.')
    return req


def final_decision(req, actor, decision, reason=''):
    """최종검수(FINAL_REVIEW) 처리. decision: 'APPROVE' | 'REVISION' | 'REJECT'."""
    if actor not in effective_approvers():
        raise PermissionDeniedError('연구소(최종 검수·반려 판단) 담당자만 처리할 수 있습니다.')
    if req.status != ReorderRequest.Status.FINAL_REVIEW:
        raise ValidationErrorWF('현재 최종검수 단계가 아닙니다.')
    if decision == 'REJECT' and not reason.strip():
        raise ValidationErrorWF('반려 시 사유는 필수입니다.')
    if decision == 'APPROVE' and not req.current_file:
        raise ValidationErrorWF('연결된 파일이 없어 승인할 수 없습니다.')

    with transaction.atomic():
        if decision == 'APPROVE':
            req.current_file.approve(actor)
            req.status = ReorderRequest.Status.APPROVED
            req.save(update_fields=['status', 'updated_at'])
            _log(req, actor, RequestEvent.Action.FINAL_APPROVE, note=reason)
            _notify(req, effective_reviewers(),
                    f'"{req.product.name}" 최종 승인되었습니다. 울산공장 전달 처리가 필요합니다.')
        elif decision in ('REVISION', 'REJECT'):
            req.status = ReorderRequest.Status.REVIEW1
            req.save(update_fields=['status', 'updated_at'])
            action = RequestEvent.Action.FINAL_REJECT if decision == 'REJECT' else RequestEvent.Action.FINAL_REVISION
            _log(req, actor, action, note=reason)
            label = '반려' if decision == 'REJECT' else '수정 필요(경미)'
            _notify(req, effective_reviewers(),
                    f'"{req.product.name}" 최종검수 결과: {label}. 사유: {reason}', kakao=(decision == 'REJECT'))
        else:
            raise ValidationErrorWF('알 수 없는 처리입니다.')
    return req


def handoff(req, actor):
    """승인된 최종파일을 요청자(울산공장)에게 전달 — 브랜드기획팀이 관리 창구로서 처리(P0-5)."""
    if actor not in effective_reviewers():
        raise PermissionDeniedError('1차 검토·관리 창구 담당자만 처리할 수 있습니다.')
    if req.status != ReorderRequest.Status.APPROVED:
        raise ValidationErrorWF('현재 전달 대기 단계가 아닙니다.')

    with transaction.atomic():
        req.status = ReorderRequest.Status.COMPLETED
        req.save(update_fields=['status', 'updated_at'])
        _log(req, actor, RequestEvent.Action.HANDOFF, note='최종파일 관리 및 울산공장 전달')
        _notify(req, [req.requester], f'"{req.product.name}" 최종파일이 전달되었습니다. 요청이 완료되었습니다.')
    return req
