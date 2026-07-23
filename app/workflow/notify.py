"""
이메일 발송 우선순위:
  1) settings.BREVO_API_KEY가 있으면 Brevo HTTPS API로 발송 (포트 차단 걱정 없음 — 추천)
  2) 없고 settings.EMAIL_HOST가 있으면 SMTP로 발송 (사내망 IP 제한 등으로 막힐 수 있음)
  3) 둘 다 없으면 콘솔/로그로만 남는 모의(mock) 발송

카카오톡/문자는 발신 API 계약(알림톡 등)이 아직 없어(PRD Open Question) 항상 로그로만
남기는 모의 발송이다 — 계약 체결 후 send_kakao_mock 내부만 실제 API 호출로 교체하면 된다.
"""
import logging

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger('workflow.notify')

BREVO_ENDPOINT = 'https://api.brevo.com/v3/smtp/email'


def is_real_email_enabled():
    return bool(settings.BREVO_API_KEY) or bool(settings.EMAIL_HOST)


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
        return

    if settings.BREVO_API_KEY:
        try:
            _send_via_brevo(recipients, subject, message)
            logger.info('[EMAIL:BREVO] to=%s subject=%s', recipients, subject)
            return
        except Exception:
            logger.exception('[EMAIL:BREVO] 발송 실패 — 로그로만 남김. to=%s subject=%s', recipients, subject)
    elif settings.EMAIL_HOST:
        try:
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
            logger.info('[EMAIL:SMTP] to=%s subject=%s', recipients, subject)
            return
        except Exception:
            logger.exception('[EMAIL:SMTP] 발송 실패 — 로그로만 남김. to=%s subject=%s', recipients, subject)

    logger.info('[MOCK EMAIL] to=%s subject=%s body=%s', recipients, subject, message)


def send_kakao_mock(users, message):
    for u in users:
        logger.info('[MOCK KAKAO/SMS] to=%s message=%s', u.username, message)
