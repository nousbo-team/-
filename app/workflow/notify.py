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

두 함수 모두 (status, detail) 튜플을 반환한다. status는 'sent' | 'failed' | 'mock'
(recipients가 아예 없으면 'no_recipients') — 이력 타임라인에 채널별 성공 여부를
표시하기 위해 services.py에서 사용한다.
"""
import hashlib
import hmac
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
