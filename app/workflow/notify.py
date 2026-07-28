"""
이메일 발송 우선순위:
  1) settings.BREVO_API_KEY가 있으면 Brevo HTTPS API로 발송 (포트 차단 걱정 없음 — 추천)
  2) 없고 settings.EMAIL_HOST가 있으면 SMTP로 발송 (사내망 IP 제한 등으로 막힐 수 있음)
  3) 둘 다 없으면 콘솔/로그로만 남는 모의(mock) 발송

카카오톡/문자는 발신 API 계약(알림톡 등)이 아직 없어(PRD Open Question) 항상 로그로만
남기는 모의 발송이다 — 계약 체결 후 send_kakao_mock 내부만 실제 API 호출로 교체하면 된다.

두 함수 모두 (status, detail) 튜플을 반환한다. status는 'sent' | 'failed' | 'mock'
(recipients가 아예 없으면 'no_recipients') — 이력 타임라인에 채널별 성공 여부를
표시하기 위해 services.py에서 사용한다.
"""
import logging

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger('workflow.notify')

BREVO_ENDPOINT = 'https://api.brevo.com/v3/smtp/email'


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
    for u in users:
        logger.info('[MOCK KAKAO/SMS] to=%s message=%s', u.username, message)
    return 'mock', ''
