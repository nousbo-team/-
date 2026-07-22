from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.models import UserProfile

from . import services
from .forms import DesignUploadForm, NewRequestForm
from .models import ReorderRequest


@login_required
def dashboard(request):
    profile = request.user.profile
    role = profile.role
    Status = ReorderRequest.Status

    my_requests = None
    pending = None
    empty_hint = None

    if role == UserProfile.Role.REQUESTER:
        my_requests = ReorderRequest.objects.filter(requester=request.user).select_related('product')
    elif role == UserProfile.Role.REVIEWER:
        if request.user in services.effective_reviewers():
            pending = ReorderRequest.objects.filter(
                status__in=[Status.REVIEW1, Status.APPROVED]
            ).select_related('product')
        else:
            pending = ReorderRequest.objects.none()
            empty_hint = '현재 담당 리뷰어가 활성 상태입니다. 담당자가 부재중으로 설정하면 이 목록에 대기건이 표시됩니다.'
    elif role == UserProfile.Role.DESIGNER:
        pending = ReorderRequest.objects.filter(status=Status.DESIGN_EDIT).select_related('product')
    elif role == UserProfile.Role.APPROVER:
        pending = ReorderRequest.objects.filter(status=Status.FINAL_REVIEW).select_related('product')

    return render(request, 'workflow/dashboard.html', {
        'role': role,
        'my_requests': my_requests,
        'pending': pending,
        'empty_hint': empty_hint,
        'stale_cutoff': timezone.now() - timedelta(days=3),
    })


@login_required
def new_request(request):
    if request.user.profile.role != UserProfile.Role.REQUESTER:
        messages.error(request, '요청자(울산공장)만 재발주를 등록할 수 있습니다.')
        return redirect('workflow:dashboard')

    if request.method == 'POST':
        form = NewRequestForm(request.POST)
        if form.is_valid():
            req, existing = services.create_request(
                form.cleaned_data['product'], request.user, form.cleaned_data['reason'])
            if existing:
                messages.warning(
                    request,
                    f'"{existing.product.name}"에 대해 이미 진행중인 건이 있습니다 ({existing.request_no}). 중복 등록을 막기 위해 기존 건으로 이동합니다.')
                return redirect('workflow:request_detail', pk=existing.pk)
            messages.success(request, '재발주 요청을 등록했습니다.')
            return redirect('workflow:request_detail', pk=req.pk)
    else:
        form = NewRequestForm()
    return render(request, 'workflow/request_new.html', {'form': form})


@login_required
def request_detail(request, pk):
    req = get_object_or_404(
        ReorderRequest.objects.select_related('product', 'requester', 'current_file'), pk=pk)
    events = req.events.select_related('actor')

    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'review_confirm':
                use_exception = request.POST.get('use_exception') == 'on'
                services.review_decision(req, request.user, 'CONFIRM_FINAL', use_exception=use_exception)
                messages.success(request, '최종본 확인 처리했습니다.')
            elif action == 'review_edit':
                note = request.POST.get('note', '')
                services.review_decision(req, request.user, 'NEEDS_EDIT', note=note)
                messages.success(request, '디자인 수정을 요청했습니다.')
            elif action == 'design_upload':
                form = DesignUploadForm(request.POST, request.FILES)
                if form.is_valid():
                    services.design_upload(
                        req, request.user, form.cleaned_data['ai_file'], form.cleaned_data['jpg_file'],
                        note=form.cleaned_data['note'])
                    messages.success(request, '수정 파일을 업로드했습니다.')
                else:
                    messages.error(request, 'AI/JPG 파일을 모두 첨부해주세요.')
            elif action == 'final_approve':
                services.final_decision(req, request.user, 'APPROVE')
                messages.success(request, '최종 승인 처리했습니다.')
            elif action == 'final_revision':
                reason = request.POST.get('reason', '')
                services.final_decision(req, request.user, 'REVISION', reason=reason)
                messages.success(request, '수정 필요로 처리했습니다.')
            elif action == 'final_reject':
                reason = request.POST.get('reason', '')
                services.final_decision(req, request.user, 'REJECT', reason=reason)
                messages.success(request, '반려 처리했습니다.')
            elif action == 'handoff':
                services.handoff(req, request.user)
                messages.success(request, '최종파일을 전달하고 완료 처리했습니다.')
            else:
                messages.error(request, '알 수 없는 요청입니다.')
        except services.PermissionDeniedError as e:
            messages.error(request, str(e))
        except services.ValidationErrorWF as e:
            messages.error(request, str(e))
        return redirect('workflow:request_detail', pk=pk)

    return render(request, 'workflow/request_detail.html', {
        'req': req,
        'events': events,
        'design_form': DesignUploadForm(),
        'is_reviewer': request.user in services.effective_reviewers(),
        'is_designer': getattr(request.user.profile, 'role', None) == UserProfile.Role.DESIGNER,
        'is_approver': request.user in services.effective_approvers(),
        'exception_available': bool(req.current_file and req.current_file.within_exception_window()),
    })


@login_required
def notifications(request):
    notifs = list(request.user.notifications.select_related('request', 'request__product'))
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return render(request, 'workflow/notifications.html', {'notifs': notifs})
