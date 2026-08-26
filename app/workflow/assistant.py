"""AI 비서(채팅형 Q&A) — 구글 Gemini API를 REST로 직접 호출한다(SDK 미설치, 다른
연동들과 동일하게 requests만 사용). 현재 로그인한 사용자가 화면에서 볼 수 있는
범위의 DB 현황(발주 건·이력·파일)만 텍스트로 요약해 Gemini에 넘기고, 그 안에서만
답하도록 지시한다 — 조회(질문·답변) 전용이며, 승인/취소 등 실제 조작은 하지 않는다.

체감 속도를 위해 기본 경로는 스트리밍(ask_stream)이다 — 답변이 다 만들어질 때까지
기다리지 않고 생성되는 대로 화면에 흘려보낸다. 여기에 더해 (1) 컨텍스트를 짧게 유지하고
(2) 생각(thinking) 예산을 0으로 둬서 첫 글자가 나오기까지의 지연을 줄인다.

settings.GEMINI_API_KEY가 비어 있으면 이 기능 자체가 화면(nav)에 노출되지 않는다.
"""
import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

from accounts.models import UserProfile, get_profile

from .models import ReorderRequest

logger = logging.getLogger('workflow.assistant')

GEMINI_ENDPOINT_TMPL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
GEMINI_STREAM_ENDPOINT_TMPL = (
    'https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse')

# Gemini에 한 번에 넘기는 발주 건 수 제한 — 컨텍스트가 길수록 응답이 느려지고 요금도
# 늘어난다. 화면에서 실제로 물어보는 건 대개 최근 건이라 이 정도면 충분하다.
_MAX_ROWS = 40
# 요청사항(detail)은 길게 쓰인 경우가 많아 앞부분만 싣는다 — 상세 전문이 필요하면
# 비서가 요청번호를 알려주고 사용자가 상세 화면에서 보면 된다.
_MAX_DETAIL_CHARS = 80
# 대화 맥락은 최근 몇 턴만 — 앞선 대화 전체를 매번 다시 보내면 갈수록 느려진다.
_MAX_HISTORY_TURNS = 6


class AssistantError(Exception):
    """설정 누락(키 없음)이나 Gemini 호출 실패 등, 답변을 만들 수 없을 때."""


def _role_scoped_requests(user):
    """history/dashboard와 동일한 규칙 — 요청자는 본인 건만, 그 외 역할은 회사 전체."""
    profile = get_profile(user)
    qs = ReorderRequest.objects.select_related('product', 'requester', 'current_file').order_by('-updated_at')
    if profile and profile.role == UserProfile.Role.REQUESTER:
        qs = qs.filter(requester=user)
    return qs


def build_context_text(user):
    """Gemini에 넘길 "현재 DB 현황" 요약 텍스트를 만든다. 사람이 화면에서 보는 것과
    같은 범위(역할별 스코프)만 담아, 이 비서가 권한 밖의 정보를 answer에 흘리지 않게 한다."""
    profile = get_profile(user)
    role_label = profile.get_role_display() if profile else '(프로필 없음)'
    qs = _role_scoped_requests(user)[:_MAX_ROWS]

    lines = [
        f'오늘 날짜: {timezone.now().strftime("%Y-%m-%d")}',
        f'질문하는 사람: {user.get_full_name() or user.username} ({role_label})',
        '',
        f'아래는 이 사람이 화면에서 볼 수 있는 발주 건 목록이다(최근 업데이트순, 최대 {_MAX_ROWS}건):',
    ]
    if not qs:
        lines.append('(해당하는 발주 건이 없음)')
    for r in qs:
        file_part = ''
        if r.current_file:
            file_part = f', 현재 파일 v{r.current_file.version}({r.current_file.get_status_display()})'
        detail = (r.detail or '').replace('\n', ' ').strip()
        if len(detail) > _MAX_DETAIL_CHARS:
            detail = detail[:_MAX_DETAIL_CHARS] + '…'
        detail_part = f', 요청사항: {detail}' if detail else ''
        lines.append(
            f'- [{r.request_no}] {r.product.name}({r.product.code}) · 상태: {r.get_status_display()} '
            f'· 사유: {r.get_reason_display()} · 요청자: {r.requester.get_full_name() or r.requester.username} '
            f'· 등록일: {r.created_at.strftime("%Y-%m-%d")} · 최종수정: {r.updated_at.strftime("%Y-%m-%d")}'
            f'{file_part}{detail_part}'
        )
    return '\n'.join(lines)


_SYSTEM_INSTRUCTION = (
    '너는 누보 포장지 발주관리 시스템의 AI 비서다. 사용자의 질문에 답할 때 반드시 '
    '아래에 함께 주어지는 "현재 DB 현황" 안의 내용만 근거로 답하고, 그 안에 없는 내용은 '
    '추측하지 말고 "현재 조회 범위에서는 확인할 수 없습니다"라고 답해라. '
    '너는 조회(질문·답변)만 하며, 발주 건을 승인·취소·수정하는 등의 실제 조작은 할 수 '
    '없다 — 그런 요청을 받으면 해당 화면(대시보드/발주요청)에서 직접 처리해야 한다고 안내해라. '
    '답변은 한국어로, 군더더기 없이 짧게 한다. 여러 건을 나열할 때만 "- " 목록을 쓰고, '
    '한두 건이면 한 문장으로 답해라. 요청번호는 반드시 그대로 적어준다.'
)


def _build_payload(user, question, history):
    contents = []
    for turn in (history or [])[-_MAX_HISTORY_TURNS:]:
        role = 'model' if turn.get('role') == 'model' else 'user'
        contents.append({'role': role, 'parts': [{'text': turn.get('text', '')}]})

    context_text = build_context_text(user)
    contents.append({
        'role': 'user',
        'parts': [{'text': f'[현재 DB 현황]\n{context_text}\n\n[질문]\n{question}'}],
    })

    return {
        'contents': contents,
        'systemInstruction': {'parts': [{'text': _SYSTEM_INSTRUCTION}]},
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 800,
            # 이 비서는 주어진 목록을 읽고 추려주는 단순한 일만 한다 — 모델이 답하기 전에
            # 따로 "생각"하는 시간을 두면 첫 글자가 나오기까지가 눈에 띄게 느려진다.
            'thinkingConfig': {'thinkingBudget': 0},
        },
    }


def _headers(api_key):
    return {'Content-Type': 'application/json', 'x-goog-api-key': api_key}


def _require_key():
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise AssistantError('AI 비서 기능이 설정되어 있지 않습니다(관리자에게 문의하세요).')
    return api_key


def _strip_thinking_config(payload):
    """thinkingConfig를 지원하지 않는 모델은 400으로 거절한다 — 그 경우 이 옵션만 빼고
    한 번 더 시도할 수 있도록 사본을 만들어 돌려준다(없으면 None)."""
    config = payload.get('generationConfig', {})
    if 'thinkingConfig' not in config:
        return None
    retry = dict(payload)
    retry['generationConfig'] = {k: v for k, v in config.items() if k != 'thinkingConfig'}
    return retry


def _extract_text(data):
    """generateContent / streamGenerateContent 응답 한 덩어리에서 텍스트만 뽑는다."""
    try:
        parts = data['candidates'][0]['content']['parts']
    except (KeyError, IndexError, TypeError):
        return ''
    return ''.join(p.get('text', '') for p in parts if isinstance(p, dict))


def ask(user, question, history=None):
    """question에 대한 답 전체를 한 번에 받아 문자열로 반환한다(비스트리밍).
    history는 [{'role': 'user'|'model', 'text': '...'}, ...] 형태의 이전 대화(선택)."""
    api_key = _require_key()
    payload = _build_payload(user, question, history)
    url = GEMINI_ENDPOINT_TMPL.format(model=settings.GEMINI_MODEL)

    def _post(body):
        return requests.post(url, json=body, timeout=30, headers=_headers(api_key))

    try:
        resp = _post(payload)
        if resp.status_code == 400:
            retry = _strip_thinking_config(payload)
            if retry is not None:
                resp = _post(retry)
    except requests.RequestException:
        logger.exception('Gemini API 호출 실패')
        raise AssistantError('AI 비서 서버 호출 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.')

    if resp.status_code != 200:
        logger.error('Gemini API 오류 응답: %s %s', resp.status_code, resp.text[:500])
        raise AssistantError('AI 비서가 응답하지 못했습니다. 잠시 후 다시 시도해주세요.')

    text = _extract_text(resp.json())
    if not text.strip():
        raise AssistantError('AI 비서가 빈 답변을 반환했습니다.')
    return text.strip()


def ask_stream(user, question, history=None):
    """답변을 생성되는 대로 조각조각(제너레이터) 내보낸다 — 화면에서 타이핑되듯 보여
    체감 대기 시간을 크게 줄인다. 실패 시 AssistantError를 던진다(첫 조각을 내보내기
    전이라면 뷰에서 그대로 오류로 처리할 수 있다)."""
    api_key = _require_key()
    payload = _build_payload(user, question, history)
    url = GEMINI_STREAM_ENDPOINT_TMPL.format(model=settings.GEMINI_MODEL)

    def _post(body):
        # (연결 타임아웃, 조각 사이 최대 대기) — 스트리밍이라 전체 시간 제한은 두지 않는다.
        return requests.post(url, json=body, timeout=(10, 60), headers=_headers(api_key), stream=True)

    try:
        resp = _post(payload)
        if resp.status_code == 400:
            retry = _strip_thinking_config(payload)
            if retry is not None:
                resp.close()
                resp = _post(retry)
    except requests.RequestException:
        logger.exception('Gemini 스트리밍 호출 실패')
        raise AssistantError('AI 비서 서버 호출 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.')

    if resp.status_code != 200:
        logger.error('Gemini 스트리밍 오류 응답: %s %s', resp.status_code, resp.text[:500])
        resp.close()
        raise AssistantError('AI 비서가 응답하지 못했습니다. 잠시 후 다시 시도해주세요.')

    sent_any = False
    try:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith('data:'):
                continue
            chunk = raw[len('data:'):].strip()
            if not chunk or chunk == '[DONE]':
                continue
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            text = _extract_text(data)
            if text:
                sent_any = True
                yield text
    except requests.RequestException:
        logger.exception('Gemini 스트리밍 도중 연결 끊김')
        if not sent_any:
            raise AssistantError('AI 비서 응답을 받는 중 연결이 끊겼습니다. 다시 시도해주세요.')
    finally:
        resp.close()

    if not sent_any:
        raise AssistantError('AI 비서가 빈 답변을 반환했습니다.')
