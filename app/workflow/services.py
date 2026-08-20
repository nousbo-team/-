"""
승인 워크플로우 상태 전이를 담당하는 서비스 레이어(P0-4). 뷰는 이 함수들만 호출하고
직접 status를 바꾸지 않는다 — 권한 검사·이력 기록·알림 발송을 한 곳에서 보장하기 위함.
"""
from django.db import transaction
from django.utils import timezone

from accounts.models import UserProfile
from catalog.models import PackagingFile

from .models import Notification, RequestEvent, ReorderRequest
from .notify import send_email_mock, send_kakao_mock

_NOTIFY_STATUS_LABEL = {
    'sent': '발송 성공',
    'failed': '발송 실패(로그 확인 필요)',
    'mock': '모의 — 실제 미발송',
    'no_recipients': '수신자 없음',
}


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
    """알림 발송. 이메일(항상 시도) + 카카오톡/문자(kakao=True일 때만)를 한 건의
    이력 항목으로 합쳐 기록한다 — 수신자 아이디와 채널별 성공 여부를 함께 남긴다."""
    for u in users:
        Notification.objects.create(user=u, request=req, message=message)

    recipient_names = ', '.join(u.username for u in users) or '(대상 없음)'
    email_status, email_detail = send_email_mock(
        users, f'[{req.request_no}] {req.product.name}', message)

    lines = [f'수신자: {recipient_names}', message, '']
    email_line = f'· 이메일: {_NOTIFY_STATUS_LABEL[email_status]}'
    if email_detail:
        email_line += f' ({email_detail})'
    lines.append(email_line)

    if kakao:
        kakao_status, _ = send_kakao_mock(users, message)
        lines.append(f'· 카카오톡/문자: {_NOTIFY_STATUS_LABEL[kakao_status]}')

    RequestEvent.objects.create(
        request=req, actor=None, action=RequestEvent.Action.NOTIFY,
        note='\n'.join(lines), channel=RequestEvent.Channel.SYSTEM,
    )


def _notify_history(req, message, kakao=False):
    """요청 취소·이전단계 반려 시, 요청자와 지금까지 이 건을 처리했던 모든 담당자
    (본인 포함)에게 알린다 — 되돌려진 사실을 이전 담당자들도 알아야 하므로."""
    participants = {req.requester}
    participants.update(e.actor for e in req.events.all() if e.actor)
    _notify(req, list(participants), message, kakao=kakao)


def create_request(product, requester, reason, detail=''):
    """재발주 요청 등록(P0-3). 동일 제품에 진행중인 건이 있으면 (None, 기존건)을 반환."""
    existing = product.has_open_request()
    if existing:
        return None, existing

    with transaction.atomic():
        req = ReorderRequest.objects.create(
            request_no=_generate_request_no(),
            product=product, requester=requester, reason=reason, detail=detail,
            status=ReorderRequest.Status.REVIEW1,
            current_file=product.current_final_file(),
        )
        note = f'재발주 요청 등록 ({req.get_reason_display()}) · 최종본 확인 요청'
        if detail.strip():
            note += f'\n요청사항: {detail.strip()}'
        _log(req, requester, RequestEvent.Action.SUBMITTED, note=note)
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
    """최종검수(FINAL_REVIEW) 처리. decision: 'APPROVE' | 'REVISION' | 'REJECT'.

    반려(REJECT)는 연구소뿐 아니라 1차 검토·관리 창구(브랜드기획팀)도 판단할 수 있다.
    승인/수정필요(경미)는 여전히 연구소 전용이다.
    """
    if decision == 'REJECT':
        if actor not in effective_approvers() and actor not in effective_reviewers():
            raise PermissionDeniedError('연구소 또는 1차 검토·관리 창구 담당자만 반려할 수 있습니다.')
    elif actor not in effective_approvers():
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


def cancel_request(req, actor, reason):
    """요청 취소. 요청자 본인은 완료·취소 전 어느 단계에서나, 1차 검토·관리 창구는
    1차검토중(REVIEW1) 단계에서만 취소할 수 있다(그 이전 단계가 없으므로 반려=취소)."""
    if req.status in ReorderRequest.TERMINAL_STATUSES:
        raise ValidationErrorWF('이미 완료되었거나 취소된 건입니다.')
    is_requester = actor == req.requester
    is_reviewer_at_review1 = req.status == ReorderRequest.Status.REVIEW1 and actor in effective_reviewers()
    if not (is_requester or is_reviewer_at_review1):
        raise PermissionDeniedError('요청자 본인 또는 1차 검토·관리 창구 담당자만 취소할 수 있습니다.')
    if not reason.strip():
        raise ValidationErrorWF('취소 사유는 필수입니다.')

    with transaction.atomic():
        req.status = ReorderRequest.Status.CANCELLED
        req.save(update_fields=['status', 'updated_at'])
        _log(req, actor, RequestEvent.Action.CANCELLED, note=reason)
        _notify_history(req, f'"{req.product.name}" 재발주 건이 취소되었습니다. 사유: {reason}', kakao=True)
    return req


def design_reject(req, actor, reason):
    """디자이너가 배정된 수정 작업을 반려 — 1차검토로 되돌린다."""
    if req.status != ReorderRequest.Status.DESIGN_EDIT:
        raise ValidationErrorWF('현재 디자인 수정 단계가 아닙니다.')
    if actor not in effective_designers():
        raise PermissionDeniedError('디자인 담당자만 반려할 수 있습니다.')
    if not reason.strip():
        raise ValidationErrorWF('반려 사유는 필수입니다.')

    with transaction.atomic():
        req.status = ReorderRequest.Status.REVIEW1
        req.save(update_fields=['status', 'updated_at'])
        _log(req, actor, RequestEvent.Action.DESIGN_REJECT, note=reason)
        _notify_history(req, f'"{req.product.name}" 디자인 담당자가 반려했습니다. 사유: {reason}', kakao=True)
    return req


def revert_approval(req, actor, reason):
    """1차 검토·관리 창구가 이미 연구소 승인된(전달 대기) 건을 최종검수중으로 되돌린다."""
    if req.status != ReorderRequest.Status.APPROVED:
        raise ValidationErrorWF('현재 전달 대기 단계가 아닙니다.')
    if actor not in effective_reviewers():
        raise PermissionDeniedError('1차 검토·관리 창구 담당자만 되돌릴 수 있습니다.')
    if not reason.strip():
        raise ValidationErrorWF('되돌리기 사유는 필수입니다.')

    with transaction.atomic():
        req.status = ReorderRequest.Status.FINAL_REVIEW
        req.save(update_fields=['status', 'updated_at'])
        _log(req, actor, RequestEvent.Action.APPROVAL_REVERTED, note=reason)
        _notify_history(
            req, f'"{req.product.name}" 승인이 취소되고 최종검수 단계로 되돌아갔습니다. 사유: {reason}', kakao=True)
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
