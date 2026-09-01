"""
이메일 발송 우선순위:
  1) settings.BREVO_API_KEY가 있으면 Brevo HTTPS API로 발송 (포트 차단 걱정 없음 — 추천)
  2) 없고 settings.EMAIL_HOST가 있으면 SMTP로 발송 (사내망 IP 제한 등으로 막힐 수 있음)
  3) 둘 다 없으면 콘솔/로그로만 남는 모의(mock) 발송
  ※ send_email_mock에 req(ReorderRequest)를 넘기면 한 줄 텍스트 대신 품목·상태·요청자·
    요청사항과 "요청 상세보기" 버튼이 있는 카드형 HTML 메일(emails/notify_email.html)로
    보낸다 — 텍스트 버전도 항상 함께 실려서(멀티파트) HTML을 못 읽는 메일 클라이언트에서도
    내용은 보인다.
  ※ 수신자가 여럿이어도 한 메일에 몰아서 to로 묶지 않고, 사람마다 따로 보낸다 —
    서로의 주소가 노출되지 않고, 받는 사람 이름으로 인사말을 붙여 개인화된다.

카카오톡/문자 발송 우선순위:
  1) PPURIO_ACCOUNT/API_SECRET/SENDER_PHONE이 있으면 뿌리오(비즈뿌리오)로 발송 — 개인 휴대폰
     번호의 통신사 "번호도용문자차단서비스" 때문에 발신이 막히는 문제를 피하려고 전용
     발신번호로 전환하며 도입. 토큰(24시간 유효)은 캐시해서 재사용한다.
  2) 뿌리오 설정이 없고 SOLAPI_API_KEY/SECRET + KAKAO_PF_ID + KAKAO_TEMPLATE_ID가 모두 있으면
     솔라피 경유 카카오 알림톡으로 발송 (승인된 템플릿 필요 — 템플릿 변수는 #{name}=받는 사람
     이름, #{package_name}=품명 두 개를 쓰는 구조로 맞춰져 있다. 승인된 실제 템플릿의 변수명이
     다르면 _send_via_solapi의 kakaoOptions.variables를 그에 맞게 고쳐야 한다)
  3) 알림톡 조건이 안 채워졌어도 SOLAPI_API_KEY/SECRET + SOLAPI_SENDER_PHONE이 있으면 솔라피로
     일반 SMS/LMS 발송
  4) 위 설정이 전혀 없으면 콘솔/로그로만 남는 모의(mock) 발송
  5) 담당자 프로필에 휴대폰번호(phone_number)가 없으면 그 담당자는 애초에 수신 대상에서 제외

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

import base64

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger('workflow.notify')

BREVO_ENDPOINT = 'https://api.brevo.com/v3/smtp/email'
SOLAPI_SEND_ENDPOINT = 'https://api.solapi.com/messages/v4/send-many'
PPURIO_TOKEN_ENDPOINT = 'https://api.bizppurio.com/v1/token'
PPURIO_SEND_ENDPOINT = 'https://api.bizppurio.com/v3/message'
PPURIO_TOKEN_CACHE_KEY = 'ppurio_access_token'


class SolapiSendError(Exception):
    """Solapi가 메시지 이력에도 남기지 않고 요청 단계에서 거부한 경우(발신번호 미등록,
    잔액 부족, 요청 형식 오류 등) — 원인 문구를 그대로 담아 이력에 노출한다."""


class PpurioSendError(Exception):
    """뿌리오가 발송을 거부한 경우 — 원인 문구를 그대로 담아 이력에 노출한다."""


def _ppurio_get_token():
    """뿌리오 인증 토큰 발급(24시간 유효) — 매 발송마다 새로 받지 않도록 캐시한다."""
    cached = cache.get(PPURIO_TOKEN_CACHE_KEY)
    if cached:
        return cached

    credentials = base64.b64encode(
        f'{settings.PPURIO_ACCOUNT}:{settings.PPURIO_API_SECRET}'.encode()).decode()
    resp = requests.post(
        PPURIO_TOKEN_ENDPOINT,
        headers={'Authorization': f'Basic {credentials}', 'Content-Type': 'application/json; charset=utf-8'},
        timeout=10)
    if not resp.ok:
        raise PpurioSendError(f'{resp.status_code} 토큰 발급 실패: {resp.text}'[:300])
    data = resp.json()
    token = f"{data['type']} {data['accesstoken']}"
    # 만료(24h)보다 여유 있게 23시간만 캐시해 갱신 주기와 겹치지 않게 한다.
    cache.set(PPURIO_TOKEN_CACHE_KEY, token, timeout=23 * 3600)
    return token


def _send_via_ppurio(phones, message):
    """phones: 휴대폰번호 문자열 리스트. 수신자별로 한 건씩 발송한다."""
    token = _ppurio_get_token()
    headers = {'Authorization': token, 'Content-Type': 'application/json; charset=utf-8'}
    text = message[:2000]
    msg_type = 'sms' if len(text.encode('utf-8')) <= 90 else 'lms'

    for phone in phones:
        body = {
            'account': settings.PPURIO_ACCOUNT,
            'type': msg_type,
            'from': settings.PPURIO_SENDER_PHONE,
            'to': phone,
            'country': '82',
            'content': {msg_type: {'message': text}},
        }
        resp = requests.post(PPURIO_SEND_ENDPOINT, headers=headers, json=body, timeout=10)
        if not resp.ok:
            try:
                reason = resp.json().get('description') or resp.text
            except ValueError:
                reason = resp.text
            raise PpurioSendError(f'{resp.status_code} {reason}'[:300])
    return 'SMS' if msg_type == 'sms' else 'LMS'


def _solapi_auth_header(api_key, api_secret):
    date = datetime.now(dt_timezone.utc).isoformat()
    salt = uuid.uuid4().hex
    signature = hmac.new(api_secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    return f'HMAC-SHA256 apiKey={api_key}, date={date}, salt={salt}, signature={signature}'


def _send_via_solapi(recipients, product_name, message):
    """recipients: [(phone, name), ...]. 카카오 알림톡 설정이 갖춰져 있으면, 승인된
    템플릿 변수(#{name}=받는 사람 이름, #{package_name}=품명)를 채운 알림톡으로
    보낸다 — 자유 문구가 아니라 승인된 템플릿 문구 그대로 나가고 이 두 변수만
    치환된다. 알림톡 설정이 없으면 자유 문구 그대로 일반 SMS/LMS로 발송한다."""
    use_kakao = bool(settings.KAKAO_PF_ID and settings.KAKAO_TEMPLATE_ID)

    def _one_message(phone, name):
        base = {'to': phone, 'from': settings.SOLAPI_SENDER_PHONE}
        if use_kakao:
            base['kakaoOptions'] = {
                'pfId': settings.KAKAO_PF_ID,
                'templateId': settings.KAKAO_TEMPLATE_ID,
                'variables': {'#{name}': name, '#{package_name}': product_name},
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
        json={'messages': [_one_message(phone, name) for phone, name in recipients]}, timeout=10)
    if not resp.ok:
        # Solapi는 검증 단계에서 거부한 요청(미등록 발신번호, 잘못된 요청 등)은
        # 메시지 이력에 아예 남기지 않는다 — 응답 본문이 유일한 단서이므로 그대로
        # 예외 메시지에 실어 보낸다(운영 서버 Shell이 없어 로그를 못 보는 경우 대비).
        try:
            body = resp.json()
            reason = body.get('errorMessage') or body.get('message') or resp.text
        except ValueError:
            reason = resp.text
        raise SolapiSendError(f'{resp.status_code} {reason}'[:300])
    return '알림톡' if use_kakao else 'SMS'


def _person_label(user):
    """이름 뒤에 소속을 붙여 "박현경 (브랜드기획팀)"처럼 만든다 — 받는 사람이
    누가 보낸 요청인지 이름만으로 헷갈리지 않게."""
    if not user:
        return '시스템'
    name = user.get_full_name() or user.username
    profile = getattr(user, 'profile', None)
    dept = getattr(profile, 'department', '') if profile else ''
    return f'{name} ({dept})' if dept else name


def _trigger_context(req, trigger):
    """이 알림을 유발한 직전 처리에서, 받는 사람이 알아야 할 것만 뽑는다.

    예전에는 어느 단계의 알림이든 최초 등록자·최초 요청사항만 실려서, 디자인팀이
    받는 수정요청 메일에도 "요청자: 이정인 / 요청사항: (최초 등록 내용)"이 찍혔다.
    실제로 필요한 건 바로 앞 단계에서 누가 무엇을 요청했는지다.
    """
    if not trigger:
        return {}

    # 등록(SUBMITTED)의 note는 시스템이 조립한 문장이라, 사람이 쓴 요청사항을 그대로 보여준다.
    is_submit = trigger.action == trigger.Action.SUBMITTED
    body = (req.detail or '').strip() if is_submit else (trigger.note or '').strip()

    return {
        'from_name': _person_label(trigger.actor),
        'from_action': trigger.get_action_display(),
        'from_body': body,
        'from_at': trigger.created_at,
        'from_has_attachment': bool(trigger.attachment),
        # 등록 알림은 보낸 사람이 곧 최초 요청자라 아래 "최초 요청" 블록이 중복된다.
        'show_origin': not is_submit,
    }


def _build_notify_email_html(req, subject, message, recipient_name, trigger=None):
    """알림 이메일을 텍스트 한 줄이 아니라 받는 사람 이름으로 인사말을 붙이고,
    품목·상태와 "직전 단계에서 누가 무엇을 요청했는지", 그리고 상세 페이지 바로가기
    버튼이 있는 카드형 HTML로 만든다. req가 없으면 None을 반환해 텍스트 메일로만 보낸다."""
    if not req:
        return None
    site_url = settings.SITE_URL.rstrip('/')
    ctx = {
        'subject': subject,
        'recipient_name': recipient_name,
        'request_no': req.request_no,
        'product_code': req.product.code,
        'product_name': req.product.name,
        'status_display': req.get_status_display(),
        'requester_name': req.requester.get_full_name() or req.requester.username,
        'requester_label': _person_label(req.requester),
        'created_at': req.created_at,
        'detail': req.detail,
        'message': message,
        'url': f'{site_url}/requests/{req.pk}/',
    }
    ctx.update(_trigger_context(req, trigger))
    return render_to_string('emails/notify_email.html', ctx)


def _build_notify_email_text(req, message, recipient_name, trigger=None):
    """HTML을 못 읽는 메일 클라이언트용 본문. 카드에 담은 것과 같은 정보를 담는다."""
    lines = [f'{recipient_name}님, 안녕하세요.', '', message, '']
    ctx = _trigger_context(req, trigger) if req else {}
    if ctx:
        lines.append(f"[보낸 사람] {ctx['from_name']}")
        lines.append(f"[처리 내용] {ctx['from_action']}")
        if ctx['from_body']:
            lines.append(f"[전달 내용] {ctx['from_body']}")
        if ctx['from_has_attachment']:
            lines.append('[첨부] 참고 파일이 있습니다 — 상세 페이지에서 내려받으세요.')
        if ctx['show_origin']:
            lines.append('')
            lines.append(f"(최초 요청) {_person_label(req.requester)}"
                         f" · {req.created_at.strftime('%Y-%m-%d')}")
            if (req.detail or '').strip():
                lines.append(f"(최초 요청사항) {req.detail.strip()}")
    if req:
        site_url = settings.SITE_URL.rstrip('/')
        lines += ['', f'요청 상세보기: {site_url}/requests/{req.pk}/']
    return '\n'.join(lines)


def _send_via_brevo(to_email, to_name, subject, message, html_body=None):
    body = {
        'sender': {'email': settings.DEFAULT_FROM_EMAIL, 'name': '누보 포장지 발주관리 시스템'},
        'to': [{'email': to_email, 'name': to_name}],
        'subject': subject,
        'textContent': message,
    }
    if html_body:
        body['htmlContent'] = html_body
    resp = requests.post(
        BREVO_ENDPOINT,
        headers={
            'accept': 'application/json',
            'api-key': settings.BREVO_API_KEY,
            'content-type': 'application/json',
        },
        json=body,
        timeout=10,
    )
    resp.raise_for_status()


def _send_via_smtp(to_email, subject, message, html_body=None):
    email = EmailMultiAlternatives(subject, message, settings.DEFAULT_FROM_EMAIL, [to_email])
    if html_body:
        email.attach_alternative(html_body, 'text/html')
    email.send(fail_silently=False)


def send_email_mock(users, subject, message, req=None, trigger=None):
    """수신자마다 따로 발송한다 — 한 메일에 여러 명을 to로 묶으면 서로의 주소가
    노출되고 메시지도 똑같아진다. 대신 받는 사람 이름으로 인사말을 붙여 개인화하고,
    한 통씩 보낸다(보통 수신자가 1~2명이라 API 호출이 늘어도 부담 없음)."""
    recipients = [u for u in users if u.email]
    if not recipients:
        return 'no_recipients', ''

    if not settings.BREVO_API_KEY and not settings.EMAIL_HOST:
        for u in recipients:
            name = u.get_full_name() or u.username
            logger.info('[MOCK EMAIL] to=%s(%s) subject=%s body=%s님, 안녕하세요.\n\n%s',
                        u.username, u.email, subject, name, message)
        return 'mock', ''

    channel = 'Brevo' if settings.BREVO_API_KEY else 'SMTP'
    sent, failed = [], []
    for u in recipients:
        name = u.get_full_name() or u.username
        text_body = _build_notify_email_text(req, message, name, trigger)
        html_body = _build_notify_email_html(req, subject, message, name, trigger)
        try:
            if settings.BREVO_API_KEY:
                _send_via_brevo(u.email, name, subject, text_body, html_body)
            else:
                _send_via_smtp(u.email, subject, text_body, html_body)
            sent.append(u.username)
        except Exception:
            logger.exception('[EMAIL:%s] 발송 실패 — 로그로만 남김. to=%s subject=%s', channel, u.email, subject)
            failed.append(u.username)

    logger.info('[EMAIL:%s] sent=%s failed=%s subject=%s', channel, sent, failed, subject)
    if not sent:
        return 'failed', channel
    if failed:
        return 'sent', f'{channel} · {len(failed)}명 실패'
    return 'sent', channel


def send_kakao_mock(users, message, req=None):
    """req가 있으면 승인된 알림톡 템플릿의 #{package_name} 변수를 채울 품명으로
    쓴다(req 없이는 알림톡 발송이 의미가 없으므로 SMS 자유 문구로만 보내진다)."""
    recipients = []
    for u in users:
        profile = getattr(u, 'profile', None)
        phone = getattr(profile, 'phone_number', '') if profile else ''
        if phone:
            name = u.get_full_name() or u.username
            recipients.append((u, phone, name))
    if not recipients:
        return 'no_recipients', ''

    phones = [phone for _, phone, _ in recipients]
    phones_with_names = [(phone, name) for _, phone, name in recipients]
    product_name = req.product.name if req else ''

    if settings.PPURIO_ACCOUNT and settings.PPURIO_API_SECRET and settings.PPURIO_SENDER_PHONE:
        try:
            channel = _send_via_ppurio(phones, message)
            logger.info('[KAKAO/SMS:PPURIO:%s] to=%s', channel, phones)
            return 'sent', channel
        except PpurioSendError as e:
            logger.exception('[KAKAO/SMS:PPURIO] 발송 실패. to=%s', phones)
            return 'failed', str(e)
        except Exception as e:
            logger.exception('[KAKAO/SMS:PPURIO] 발송 실패. to=%s', phones)
            return 'failed', f'{type(e).__name__}: {e}'[:300]

    if settings.SOLAPI_API_KEY and settings.SOLAPI_API_SECRET and settings.SOLAPI_SENDER_PHONE:
        try:
            channel = _send_via_solapi(phones_with_names, product_name, message)
            logger.info('[KAKAO/SMS:SOLAPI:%s] to=%s', channel, phones)
            return 'sent', channel
        except SolapiSendError as e:
            logger.exception('[KAKAO/SMS:SOLAPI] 발송 실패. to=%s', phones)
            return 'failed', str(e)
        except Exception as e:
            logger.exception('[KAKAO/SMS:SOLAPI] 발송 실패. to=%s', phones)
            return 'failed', f'{type(e).__name__}: {e}'[:300]

    for u, phone, _ in recipients:
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
