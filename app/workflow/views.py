import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import models
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import UserProfile, get_profile

from . import services
from .forms import DesignUploadForm, NewRequestForm
from .models import PushSubscription, ReorderRequest, RequestEvent


def guide(request):
    """역할별 사용 매뉴얼 — 로그인 없이 누구나 볼 수 있는 공개 안내 페이지."""
    return render(request, 'guide.html')


_SERVICE_WORKER_JS = """
self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  var title = data.title || '누보 포장지 발주관리 시스템';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    data: { url: data.url || '/' }
  }));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
});
"""


def service_worker(request):
    """루트 경로(/sw.js)에서 서빙해야 사이트 전체를 제어 범위로 등록할 수 있다
    (하위 경로에서 서빙하면 그 경로 밑으로만 적용됨) — 그래서 static이 아닌
    전용 뷰로 둔다."""
    return HttpResponse(_SERVICE_WORKER_JS, content_type='application/javascript')


@login_required
@require_POST
def push_subscribe(request):
    try:
        data = json.loads(request.body)
        PushSubscription.objects.update_or_create(
            endpoint=data['endpoint'],
            defaults={'user': request.user, 'p256dh': data['keys']['p256dh'], 'auth': data['keys']['auth']},
        )
    except (KeyError, ValueError, TypeError):
        return JsonResponse({'ok': False}, status=400)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def push_unsubscribe(request):
    try:
        data = json.loads(request.body)
    except ValueError:
        data = {}
    PushSubscription.objects.filter(endpoint=data.get('endpoint', '')).delete()
    return JsonResponse({'ok': True})


@login_required
def reset_test_data(request):
    """테스트로 쌓인 발주 건·이력·알림·품목·파일을 지우고 데모 데이터를 다시 채운다.
    Render 무료 플랜은 Shell이 없어 관리자(nousbo)가 브라우저에서 직접 실행할 수 있도록
    만든 화면 — reset_data 관리 명령을 그대로 호출한다. 계정(User/UserProfile)은 건드리지
    않는다."""
    if not request.user.is_superuser:
        raise PermissionDenied('관리자 계정만 사용할 수 있습니다.')
    if request.method == 'POST':
        call_command('reset_data')
        messages.success(
            request,
            '테스트 데이터(발주 건·이력·알림·품목·파일)를 초기화하고 데모 데이터를 다시 채웠습니다. 계정 정보는 그대로입니다.')
        return redirect('workflow:dashboard')
    return render(request, 'workflow/reset_confirm.html')


@login_required
def seed_demo(request):
    """데모 계정(haon 등) 5명 + 관리자 + 샘플 품목 3건을 (없으면) 다시 만든다.
    예전엔 배포 빌드마다 자동 실행됐지만, 실제 운영 데이터가 쌓인 뒤로는 배포할
    때마다 데모 품목이 되살아나고 계정 비밀번호가 재설정되는 부작용이 있어 빌드에서
    뺐다 — 필요할 때만 관리자가 여기서 수동으로 실행한다."""
    if not request.user.is_superuser:
        raise PermissionDenied('관리자 계정만 사용할 수 있습니다.')
    if request.method == 'POST':
        call_command('seed_demo_data')
        messages.success(
            request,
            '데모 계정(haon, isis9, shindeok_kim, guychj, hjcho, nousbo)과 샘플 품목 3건을 확인·생성했습니다. 기존 데이터는 지우지 않습니다.')
        return redirect('workflow:dashboard')
    return render(request, 'workflow/seed_confirm.html')


@login_required
def dashboard(request):
    profile = get_profile(request.user)
    if not profile:
        return render(request, 'workflow/no_profile.html')
    role = profile.role
    Status = ReorderRequest.Status

    my_requests = None
    pending = None
    empty_hint = None
    all_active = None

    if role == UserProfile.Role.REQUESTER:
        my_requests = ReorderRequest.objects.filter(
            requester=request.user
        ).exclude(status__in=ReorderRequest.TERMINAL_STATUSES).select_related('product')
    elif role == UserProfile.Role.REVIEWER:
        if request.user in services.effective_reviewers():
            pending = ReorderRequest.objects.filter(
                status__in=[Status.REVIEW1, Status.APPROVED]
            ).select_related('product')
        else:
            pending = ReorderRequest.objects.none()
            empty_hint = '현재 담당 리뷰어가 활성 상태입니다. 담당자가 부재중으로 설정하면 이 목록에 대기건이 표시됩니다.'
        # 1차 검토·관리 창구는 지금 내 차례가 아니어도 회사 전체 건이 어디까지 왔는지
        # 항상 확인할 수 있어야 한다 — 처리할 건이 없다고 화면이 텅 비지 않도록 별도로 노출.
        all_active = ReorderRequest.objects.exclude(
            status__in=ReorderRequest.TERMINAL_STATUSES
        ).select_related('product', 'requester').order_by('-updated_at')
    elif role == UserProfile.Role.DESIGNER:
        pending = ReorderRequest.objects.filter(status=Status.DESIGN_EDIT).select_related('product')
        # 디자인팀 입장에서 "요청자"는 최초 발주요청을 올린 울산공장이 아니라, 지금 이
        # 수정 지시를 내린 1차 검토·관리 창구 담당자(박현경 팀장 또는 부재 시 대체자
        # 김신덕 본부장)다 — 건마다 가장 최근 수정 요청 이벤트의 처리자를 붙여준다.
        for r in pending:
            last_edit = r.events.filter(
                action=RequestEvent.Action.REVIEW_REQUEST_EDIT
            ).select_related('actor').order_by('-created_at').first()
            r.assigned_by = last_edit.actor if last_edit else None
    elif role == UserProfile.Role.APPROVER:
        pending = ReorderRequest.objects.filter(status=Status.FINAL_REVIEW).select_related('product')

    return render(request, 'workflow/dashboard.html', {
        'role': role,
        'my_requests': my_requests,
        'pending': pending,
        'empty_hint': empty_hint,
        'all_active': all_active,
        'stale_cutoff': timezone.now() - timedelta(days=3),
    })


@login_required
def history(request):
    """완료/취소된 발주 건 이력 — 요청번호·품목명 키워드 검색 + 상태 필터 + 페이지네이션.
    대시보드가 진행중인 건만 보여주도록 분리되면서, 지난 이력을 따로 찾아볼 수 있게 만든 화면.
    요청자는 본인이 등록한 건만, 그 외 역할(리뷰어/디자인/연구소)은 회사 전체 이력을 본다."""
    from django.core.paginator import Paginator

    profile = get_profile(request.user)
    if not profile:
        return render(request, 'workflow/no_profile.html')

    Status = ReorderRequest.Status
    qs = ReorderRequest.objects.filter(
        status__in=ReorderRequest.TERMINAL_STATUSES
    ).select_related('product', 'requester')
    if profile.role == UserProfile.Role.REQUESTER:
        qs = qs.filter(requester=request.user)

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            models.Q(request_no__icontains=q) | models.Q(product__name__icontains=q)
            | models.Q(product__code__icontains=q)
        )
    status_filter = request.GET.get('status', '')
    if status_filter in (Status.COMPLETED, Status.CANCELLED):
        qs = qs.filter(status=status_filter)

    qs = qs.order_by('-updated_at')
    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'workflow/history.html', {
        'page_obj': page_obj,
        'q': q,
        'status_filter': status_filter,
    })


@login_required
def new_request(request):
    profile = get_profile(request.user)
    if not profile or profile.role != UserProfile.Role.REQUESTER:
        messages.error(request, '요청자(울산공장)만 발주요청을 등록할 수 있습니다.')
        return redirect('workflow:dashboard')

    if request.method == 'POST':
        form = NewRequestForm(request.POST)
        if form.is_valid():
            req, existing = services.create_request(
                form.cleaned_data['product'], request.user, form.cleaned_data['reason'],
                detail=form.cleaned_data['detail'])
            if existing:
                messages.warning(
                    request,
                    f'"{existing.product.name}"에 대해 이미 진행중인 건이 있습니다 ({existing.request_no}). 중복 등록을 막기 위해 기존 건으로 이동합니다.')
                return redirect('workflow:request_detail', pk=existing.pk)
            messages.success(request, '발주요청을 등록했습니다.')
            return redirect('workflow:request_detail', pk=req.pk)
    else:
        form = NewRequestForm()
    return render(request, 'workflow/request_new.html', {'form': form})


@login_required
def download_attachment(request, pk):
    """catalog.views.download_packaging_file과 같은 이유 — 참고 파일 링크를
    Supabase Storage 서명 URL로 바로 걸면 교차 출처라 브라우저가 <a download="...">
    을 무시해 한글 파일명이 저장되지 않는다. 이 서버를 거쳐 내려주면 같은 출처가
    되어 Content-Disposition의 파일명이 그대로 적용된다."""
    event = get_object_or_404(RequestEvent, pk=pk)
    if not event.attachment:
        raise Http404('첨부 파일이 없습니다.')
    event.attachment.open('rb')
    return FileResponse(event.attachment, as_attachment=True, filename=event.attachment_filename)


@login_required
def request_detail(request, pk):
    req = get_object_or_404(
        ReorderRequest.objects.select_related('product', 'requester', 'current_file'), pk=pk)
    events = req.events.select_related('actor').order_by('-created_at')

    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'review_confirm':
                use_exception = request.POST.get('use_exception') == 'on'
                services.review_decision(req, request.user, 'CONFIRM_FINAL', use_exception=use_exception)
                messages.success(request, '최종본 확인 처리했습니다.')
            elif action == 'review_edit':
                note = request.POST.get('note', '')
                attachment = request.FILES.get('attachment')
                services.review_decision(req, request.user, 'NEEDS_EDIT', note=note, attachment=attachment)
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
            elif action == 'cancel':
                reason = request.POST.get('reason', '')
                services.cancel_request(req, request.user, reason)
                messages.success(request, '요청을 취소했습니다.')
            elif action == 'design_reject':
                reason = request.POST.get('reason', '')
                services.design_reject(req, request.user, reason)
                messages.success(request, '디자인 작업을 반려하고 1차검토로 되돌렸습니다.')
            elif action == 'revert_approval':
                reason = request.POST.get('reason', '')
                services.revert_approval(req, request.user, reason)
                messages.success(request, '승인을 취소하고 최종검수 단계로 되돌렸습니다.')
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
        'is_designer': getattr(get_profile(request.user), 'role', None) == UserProfile.Role.DESIGNER,
        'is_approver': request.user in services.effective_approvers(),
        'exception_available': bool(req.current_file and req.current_file.within_exception_window()),
    })


@login_required
def notifications(request):
    notifs = list(request.user.notifications.select_related('request', 'request__product'))
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return render(request, 'workflow/notifications.html', {'notifs': notifs})
