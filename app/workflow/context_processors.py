from django.conf import settings


def unread_notifications(request):
    if request.user.is_authenticated:
        count = request.user.notifications.filter(is_read=False).count()
        return {'unread_notification_count': count}
    return {}


def web_push(request):
    """base.html의 "브라우저 알림 받기" 버튼에서 쓸 VAPID 공개키. 서버에 키가
    설정돼 있을 때만 값이 채워지고, 없으면 버튼 자체가 화면에 나타나지 않는다."""
    return {'vapid_public_key': settings.VAPID_PUBLIC_KEY}


def ai_assistant(request):
    """base.html의 "AI 비서" 메뉴 노출 여부. GEMINI_API_KEY가 설정돼 있을 때만
    메뉴가 나타난다(키가 없으면 기능 자체를 화면에서 감춘다)."""
    return {'ai_assistant_enabled': bool(settings.GEMINI_API_KEY)}
