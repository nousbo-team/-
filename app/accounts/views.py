from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


@login_required
def toggle_away(request):
    """리뷰어(1차 검토·관리 창구)가 부재중 여부를 토글한다 — 대체 담당자 라우팅(P0-8)."""
    if request.method == 'POST':
        profile = request.user.profile
        profile.is_away = not profile.is_away
        profile.save(update_fields=['is_away'])
        if profile.is_away:
            messages.info(request, f'부재중으로 설정했습니다. 대기건이 {profile.backup_user}에게도 노출됩니다.')
        else:
            messages.info(request, '부재중 설정을 해제했습니다.')
    return redirect('workflow:dashboard')
