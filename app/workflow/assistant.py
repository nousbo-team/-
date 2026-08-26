"""AI 비서(채팅형 Q&A) — 구글 Gemini API를 REST로 직접 호출한다(SDK 미설치, 다른
연동들과 동일하게 requests만 사용). 현재 로그인한 사용자가 화면에서 볼 수 있는
범위의 DB 현황(발주 건·이력·파일)만 텍스트로 요약해 Gemini에 넘기고, 그 안에서만
답하도록 지시한다 — 조회(질문·답변) 전용이며, 승인/취소 등 실제 조작은 하지 않는다.

settings.GEMINI_API_KEY가 비어 있으면 이 기능 자체가 화면(nav)에 노출되지 않는다.
"""
import logging

import requests
from django.conf import settings
from django.utils import timezone

from accounts.models import UserProfile, get_profile

from .models import ReorderRequest

logger = logging.getLogger('workflow.assistant')

GEMINI_ENDPOINT_TMPL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

# Gemini에 한 번에 넘기는 발주 건 수 제한 — 컨텍스트가 너무 커지는 것을 막는다
# (회사 전체 이력이 많아져도 매 질문마다 그 전부를 요약해 보낼 필요는 없다).
_MAX_ROWS = 60


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
        '아래는 이 사람이 화면에서 볼 수 있는 발주 건 목록이다(최근 업데이트순, 최대 '
        f'{_MAX_ROWS}건):',
    ]
    if not qs:
        lines.append('(해당하는 발주 건이 없음)')
    for r in qs:
        file_part = ''
        if r.current_file:
            file_part = f', 현재 파일 v{r.current_file.version}({r.current_file.get_status_display()})'
        detail = (r.detail or '').replace('\n', ' ').strip()
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
    '답변은 한국어로, 간결하게 한다.'
)


def ask(user, question, history=None):
    """question(문자열)에 대한 답을 Gemini로부터 받아 문자열로 반환한다.
    history는 [{'role': 'user'|'model', 'text': '...'}, ...] 형태의 이전 대화(선택)."""
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise AssistantError('AI 비서 기능이 설정되어 있지 않습니다(관리자에게 문의하세요).')

    contents = []
    for turn in (history or []):
        role = 'model' if turn.get('role') == 'model' else 'user'
        contents.append({'role': role, 'parts': [{'text': turn.get('text', '')}]})

    context_text = build_context_text(user)
    contents.append({
        'role': 'user',
        'parts': [{'text': f'[현재 DB 현황]\n{context_text}\n\n[질문]\n{question}'}],
    })

    payload = {
        'contents': contents,
        'systemInstruction': {'parts': [{'text': _SYSTEM_INSTRUCTION}]},
        'generationConfig': {'temperature': 0.2},
    }
    url = GEMINI_ENDPOINT_TMPL.format(model=settings.GEMINI_MODEL)
    try:
        resp = requests.post(
            url, json=payload, timeout=20,
            headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key},
        )
    except requests.RequestException:
        logger.exception('Gemini API 호출 실패')
        raise AssistantError('AI 비서 서버 호출 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.')

    if resp.status_code != 200:
        logger.error('Gemini API 오류 응답: %s %s', resp.status_code, resp.text[:500])
        raise AssistantError('AI 비서가 응답하지 못했습니다. 잠시 후 다시 시도해주세요.')

    data = resp.json()
    try:
        candidates = data['candidates']
        text = ''.join(part.get('text', '') for part in candidates[0]['content']['parts'])
    except (KeyError, IndexError, TypeError):
        logger.error('Gemini 응답 형식이 예상과 다름: %s', str(data)[:500])
        raise AssistantError('AI 비서 응답을 해석할 수 없습니다.')

    if not text.strip():
        raise AssistantError('AI 비서가 빈 답변을 반환했습니다.')
    return text.strip()
