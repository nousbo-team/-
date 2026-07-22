"""
이메일은 settings.EMAIL_HOST가 설정되어 있으면 실제 SMTP로 발송되고(각자 아이디@nousbo.com),
비어 있으면 콘솔/로그로만 남는 모의(mock) 발송으로 자동 대체된다.

카카오톡/문자는 발신 API 계약(알림톡 등)이 아직 없어(PRD Open Question) 항상 로그로만
남기는 모의 발송이다 — 계약 체결 후 send_kakao_mock 내부만 실제 API 호출로 교체하면 된다.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger('workflow.notify')


def is_real_email_enabled():
    return bool(settings.EMAIL_HOST)


def send_email_mock(users, subject, message):
    recipients = [u.email for u in users if u.email]
    if not recipients:
        return
    if is_real_email_enabled():
        try:
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
            logger.info('[EMAIL] to=%s subject=%s', recipients, subject)
            return
        except Exception:
            logger.exception('[EMAIL] 발송 실패 — 로그로만 남김. to=%s subject=%s', recipients, subject)
    logger.info('[MOCK EMAIL] to=%s subject=%s body=%s', recipients, subject, message)


def send_kakao_mock(users, message):
    for u in users:
        logger.info('[MOCK KAKAO/SMS] to=%s message=%s', u.username, message)
