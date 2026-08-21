"""
이메일 발송 우선순위:
  1) settings.BREVO_API_KEY가 있으면 Brevo HTTPS API로 발송 (포트 차단 걱정 없음 — 추천)
  2) 없고 settings.EMAIL_HOST가 있으면 SMTP로 발송 (사내망 IP 제한 등으로 막힐 수 있음)
  3) 둘 다 없으면 콘솔/로그로만 남는 모의(mock) 발송

카카오톡/문자 발송 우선순위 (솔라피 Solapi 사용):
  1) SOLAPI_API_KEY/SECRET + KAKAO_PF_ID + KAKAO_TEMPLATE_ID가 모두 있으면 카카오 알림톡으로
     발송 (승인된 템플릿 필요 — 템플릿 변수는 #{message} 하나만 쓰는 구조를 가정)
  2) 알림톡 조건이 안 채워졌어도 SOLAPI_API_KEY/SECRET + SOLAPI_SENDER_PHONE이 있으면 일반
     SMS/LMS로 발송 (사전 승인된 템플릿이 없어도 자유 문구 발송 가능 — 우선 이걸로 시작하기 좋음)
  3) 위 설정이 전혀 없으면 콘솔/로그로만 남는 모의(mock) 발송
  4) 담당자 프로필에 휴대폰번호(phone_number)가 없으면 그 담당자는 애초에 수신 대상에서 제외

웹 푸시(브라우저 알림):
  1) settings.VAPID_PUBLIC_KEY/PRIVATE_KEY가 있으면, 담당자가 "브라우저 알림 받기"를
     켜둔 기기(PushSubscription)로 실제 브라우저 알림을 보낸다. 별도 계정 가입이나
     비용 없이 브라우저 표준 기능만으로 동작한다(FCM/APNs 등은 브라우저가 내부적으로
     알아서 씀).
  2) 구독이 만료/취소된 경우(404/410) 그 구독 정보를 자동으로 정리한다.
  3) 키가 없으면 로그만 남기는 모의 발송.

세 함수 모두 (status, detail) 튜플을 반환한다. status는 'sent' | 'failed' | 'mock'
(recipients가 아예 없으면 'no_recipients') — 이력 타임라인에 채널별 성공 여부를
표시하기 위해 services.py에서 사용한다.
"""
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone as dt_timezone

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger('workflow.notify')

BREVO_ENDPOINT = 'https://api.brevo.com/v3/smtp/email'
SOLAPI_SEND_ENDPOINT = 'https://api.solapi.com/messages/v4/send-many'


def _solapi_auth_header(api_key, api_secret):
    date = datetime.now(dt_timezone.utc).isoformat()
    salt = uuid.uuid4().hex
    signature = hmac.new(api_secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    return f'HMAC-SHA256 apiKey={api_key}, date={date}, salt={salt}, signature={signature}'


def _send_via_solapi(phones, message):
    """phones: 휴대폰번호 문자열 리스트. 카카오 알림톡 설정이 갖춰져 있으면 알림톡으로,
    아니면 일반 SMS/LMS로 발송한다."""
    use_kakao = bool(settings.KAKAO_PF_ID and settings.KAKAO_TEMPLATE_ID)

    def _one_message(phone):
        base = {'to': phone, 'from': settings.SOLAPI_SENDER_PHONE}
        if use_kakao:
            base['kakaoOptions'] = {
                'pfId': settings.KAKAO_PF_ID,
                'templateId': settings.KAKAO_TEMPLATE_ID,
                'variables': {'#{message}': message},
                'disableSms': False,  # 알림톡 실패 시 문자로 자동 대체
            }
        else:
            base['text'] = message[:2000]
        return base

    headers = {
        'Authorization': _solapi_auth_header(settings.SOLAPI_API_KEY, settings.SOLAPI_API_SECRET),
        'Content-Type': 'application/json',
    }
    resp = requests.post(
        SOLAPI_SEND_ENDPOINT, headers=headers,
        json={'messages': [_one_message(p) for p in phones]}, timeout=10)
    resp.raise_for_status()
    return '알림톡' if use_kakao else 'SMS'


def _send_via_brevo(recipients, subject, message):
    resp = requests.post(
        BREVO_ENDPOINT,
        headers={
            'accept': 'application/json',
            'api-key': settings.BREVO_API_KEY,
            'content-type': 'application/json',
        },
        json={
            'sender': {'email': settings.DEFAULT_FROM_EMAIL, 'name': '누보 포장지 발주관리 시스템'},
            'to': [{'email': r} for r in recipients],
            'subject': subject,
            'textContent': message,
        },
        timeout=10,
    )
    resp.raise_for_status()


def send_email_mock(users, subject, message):
    recipients = [u.email for u in users if u.email]
    if not recipients:
        return 'no_recipients', ''

    if settings.BREVO_API_KEY:
        try:
            _send_via_brevo(recipients, subject, message)
            logger.info('[EMAIL:BREVO] to=%s subject=%s', recipients, subject)
            return 'sent', 'Brevo'
        except Exception:
            logger.exception('[EMAIL:BREVO] 발송 실패 — 로그로만 남김. to=%s subject=%s', recipients, subject)
            return 'failed', 'Brevo'
    elif settings.EMAIL_HOST:
        try:
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
            logger.info('[EMAIL:SMTP] to=%s subject=%s', recipients, subject)
            return 'sent', 'SMTP'
        except Exception:
            logger.exception('[EMAIL:SMTP] 발송 실패 — 로그로만 남김. to=%s subject=%s', recipients, subject)
            return 'failed', 'SMTP'

    logger.info('[MOCK EMAIL] to=%s subject=%s body=%s', recipients, subject, message)
    return 'mock', ''


def send_kakao_mock(users, message):
    recipients = []
    for u in users:
        profile = getattr(u, 'profile', None)
        phone = getattr(profile, 'phone_number', '') if profile else ''
        if phone:
            recipients.append((u, phone))
    if not recipients:
        return 'no_recipients', ''

    if settings.SOLAPI_API_KEY and settings.SOLAPI_API_SECRET and settings.SOLAPI_SENDER_PHONE:
        phones = [phone for _, phone in recipients]
        try:
            channel = _send_via_solapi(phones, message)
            logger.info('[KAKAO/SMS:SOLAPI:%s] to=%s', channel, phones)
            return 'sent', channel
        except Exception:
            logger.exception('[KAKAO/SMS:SOLAPI] 발송 실패 - 로그로만 남김. to=%s', phones)
            return 'failed', 'Solapi'

    for u, phone in recipients:
        logger.info('[MOCK KAKAO/SMS] to=%s(%s) message=%s', u.username, phone, message)
    return 'mock', ''


def send_web_push(users, title, message, url='/'):
    """PushSubscription을 등록해둔 담당자에게 브라우저 알림을 보낸다."""
    from .models import PushSubscription

    subs = list(PushSubscription.objects.filter(user__in=users).select_related('user'))
    if not subs:
        return 'no_recipients', ''

    if not (settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY):
        for s in subs:
            logger.info('[MOCK WEBPUSH] to=%s title=%s', s.user.username, title)
        return 'mock', ''

    from pywebpush import WebPushException, webpush

    payload = json.dumps({'title': title, 'body': message, 'url': url})
    sent = failed = 0
    for s in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': s.endpoint,
                    'keys': {'p256dh': s.p256dh, 'auth': s.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={'sub': settings.VAPID_CLAIM_EMAIL},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code in (404, 410):
                s.delete()  # 구독이 만료/해지됨 — 다음부터 대상에서 자동 제외
            else:
                logger.warning('[WEBPUSH] 발송 실패: %s (%s)', s.endpoint[:60], e)
            failed += 1
        except Exception:
            logger.exception('[WEBPUSH] 알 수 없는 오류: %s', s.endpoint[:60])
            failed += 1

    if sent and not failed:
        return 'sent', f'{sent}건'
    if sent:
        return 'sent', f'{sent}건 성공, {failed}건 실패'
    return 'failed', f'{failed}건 실패'
