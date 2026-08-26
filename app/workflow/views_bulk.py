import csv
import io
import json
import re
import zipfile
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from catalog.models import PackagingFile, Product

from . import services
from .models import ReorderRequest

PAIR_SUFFIX_RE = re.compile(r'(_ai|_jpg|_jpeg|_png|_gif|_pdf)$', re.IGNORECASE)
VISUAL_EXTENSIONS = ('jpg', 'jpeg', 'png', 'gif', 'pdf')


@login_required
def bulk_home(request):
    """catalog.views.product_list와 같은 이유로 품목별 current_final_file()을 템플릿에서
    개별 호출하면 품목 수(현재 258건)만큼 쿼리가 늘어나 워커 타임아웃(502/500)으로
    이어진다 — 최종 승인본을 한 번에 조회해 품목별로 파이썬에서 묶어 전달한다.

    258건을 한 화면에 전부 그리면 스크롤이 너무 길어지므로, 검색(품목명·품목코드)과
    페이지네이션으로 좁혀볼 수 있게 한다 — 매 페이지마다 그 페이지에 보이는 품목의
    최종 승인본만 조회하므로 전체 건수가 늘어나도 쿼리 수는 늘지 않는다."""
    q = request.GET.get('q', '').strip()
    products = Product.objects.filter(is_active=True).order_by('name')
    if q:
        products = products.filter(Q(name__icontains=q) | Q(code__icontains=q))

    paginator = Paginator(products, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    product_ids = [p.pk for p in page_obj]
    final_file_by_product = {}
    for f in (PackagingFile.objects
              .filter(product_id__in=product_ids, is_active=True,
                      status=PackagingFile.Status.FINAL_APPROVED)
              .order_by('product_id', '-version')):
        final_file_by_product.setdefault(f.product_id, f)  # 품목별로 최신 버전 하나만 남김

    rows = [{'product': p, 'final_file': final_file_by_product.get(p.pk)} for p in page_obj]

    # 일괄 업로드 화면에서 파일 묶음마다 품목을 직접 골라 연결할 수 있도록, 검색/
    # 페이지네이션과 무관하게 활성 품목 전체를 가벼운 목록(id/code/name)으로 넘긴다 —
    # 258건 수준이면 페이지 하나에 실어도 부담 없다(현재 파일 목록과는 별개).
    all_products = list(
        Product.objects.filter(is_active=True).order_by('name').values('id', 'code', 'name'))

    return render(request, 'workflow/bulk.html', {
        'rows': rows, 'page_obj': page_obj, 'q': q, 'all_products': all_products,
    })


@login_required
def bulk_upload_history(request):
    """일괄 업로드는 특정 재발주 건(ReorderRequest)에 매이지 않는 품목 파일 관리라,
    그 건의 이력(타임라인)에는 남지 않는다 — 나중에 "이 버전이 왜 갑자기 바뀌었는지"
    추적할 곳이 없어지는 문제가 있어, 일괄 업로드로 등록된 버전만 모아 별도로 보여준다."""
    q = request.GET.get('q', '').strip()
    logs = (PackagingFile.objects
            .filter(is_bulk_upload=True)
            .select_related('product', 'uploaded_by', 'approved_by')
            .order_by('-uploaded_at'))
    if q:
        logs = logs.filter(Q(product__name__icontains=q) | Q(product__code__icontains=q) | Q(note__icontains=q))

    paginator = Paginator(logs, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    # 완료·취소 이력(workflow:history)에도 같이 노출되는 재발주 건 요청번호를 함께
    # 보여준다 — create_bulk_upload_request가 PackagingFile마다 만들어둔 건이다.
    file_ids = [f.pk for f in page_obj]
    request_by_file = {
        r.current_file_id: r
        for r in ReorderRequest.objects.filter(current_file_id__in=file_ids).only('request_no', 'current_file')
    }
    for f in page_obj:
        f.linked_request = request_by_file.get(f.pk)

    return render(request, 'workflow/bulk_upload_history.html', {'page_obj': page_obj, 'q': q})


@login_required
def bulk_upload(request):
    if request.method != 'POST':
        return redirect('workflow:bulk')

    files = request.FILES.getlist('files')

    # 화면에서 파일 묶음별로 직접 고른 품목/승인 정보 — bulk.html의 JS가 파일을
    # 고르는 즉시 같은 규칙(PAIR_SUFFIX_RE)으로 그룹을 만들어 미리 보여주고, 제출
    # 시점에 이 JSON 하나로 실어 보낸다. 엑셀 매핑표 없이 화면에서 바로 지정하는
    # 방식만 지원한다 — 승인자·승인일·사유가 전부 필수라 JS가 값을 채우지 못하면
    # 애초에 제출되지 않는다.
    try:
        ui_mapping = json.loads(request.POST.get('mapping_json') or '{}')
    except ValueError:
        ui_mapping = {}

    groups = {}
    for f in files:
        stem, _, ext = f.name.rpartition('.')
        base_key = PAIR_SUFFIX_RE.sub('', stem or f.name)
        groups.setdefault(base_key, {})[ext.lower()] = f

    registered, unmatched = [], []
    for base_key, pair in groups.items():
        ai_file = pair.get('ai')
        jpg_file = next((pair[ext] for ext in VISUAL_EXTENSIONS if ext in pair), None)
        if not ai_file or not jpg_file:
            unmatched.append(f'{base_key} (AI/이미지·PDF 짝이 맞지 않음)')
            continue

        ui_row = ui_mapping.get(base_key)
        product = Product.objects.filter(pk=ui_row.get('product_id')).first() if ui_row else None
        if not product:
            unmatched.append(f'{base_key} (품목 미매칭 — 화면에서 품목을 선택하세요)')
            continue

        note = ui_row.get('note', '').strip() or '일괄 업로드'
        pkg = PackagingFile.objects.create(
            product=product, ai_file=ai_file, jpg_file=jpg_file, uploaded_by=request.user,
            note=note, is_bulk_upload=True,
        )

        approver = ui_row.get('approver', '').strip()
        raw_date = ui_row.get('approved_date') or ''
        approved_at = None
        if raw_date:
            try:
                approved_at = datetime.strptime(raw_date, '%Y-%m-%d')
            except ValueError:
                approved_at = None
        pkg.approve(request.user)
        if approved_at:
            if timezone.is_naive(approved_at):
                approved_at = timezone.make_aware(approved_at)
            pkg.approved_at = approved_at
        if approver:
            pkg.note = f'{pkg.note} · 원 승인자: {approver}'
        pkg.save(update_fields=['approved_at', 'note'])

        # 특정 재발주 건과 연결되지 않는 파일 갱신이라 "완료·취소 이력"에 안 잡히고
        # 나중에 이 버전이 왜 생겼는지 추적할 수 없었다 — 완료 상태의 재발주 건을
        # 하나씩 만들어 요청번호를 채번하고 사유를 이력에 남긴다.
        services.create_bulk_upload_request(product, request.user, pkg, pkg.note)
        registered.append(f'{product.name} v{pkg.version}')

    if registered:
        messages.success(request, f'{len(registered)}건 등록됨: ' + ', '.join(registered))
    if unmatched:
        messages.warning(request, f'{len(unmatched)}건 미매칭(등록되지 않음): ' + '; '.join(unmatched))
    if not registered and not unmatched:
        messages.error(request, '업로드할 파일을 선택해주세요.')
    return redirect('workflow:bulk')


@login_required
def bulk_download(request):
    if request.method != 'POST':
        return redirect('workflow:bulk')

    product_ids = request.POST.getlist('products')
    products = Product.objects.filter(pk__in=product_ids)
    if not products:
        messages.error(request, '다운로드할 품목을 하나 이상 선택해주세요.')
        return redirect('workflow:bulk')

    buffer = io.BytesIO()
    manifest_rows = [['품목코드', '품목명', '버전', '승인일', '승인자', '파일']]
    skipped = []
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for product in products:
            final_file = product.current_final_file()
            if not final_file:
                skipped.append(f'{product.code} {product.name}')
                continue
            folder = re.sub(r'[\\/:*?"<>|]', '_', f'{product.code}_{product.name}')
            for field, display_name in (
                (final_file.ai_file, final_file.ai_display_filename),
                (final_file.jpg_file, final_file.jpg_display_filename),
            ):
                if field:
                    # 개별 다운로드(품목명_버전_날짜.확장자)와 같은 파일명 규칙을
                    # 압축 파일 안에서도 그대로 적용한다.
                    arcname = f'{folder}/{display_name}'
                    field.open('rb')
                    zf.writestr(arcname, field.read())
                    field.close()
                    manifest_rows.append([
                        product.code, product.name, final_file.version,
                        final_file.approved_at.strftime('%Y-%m-%d') if final_file.approved_at else '',
                        final_file.approved_by.get_full_name() if final_file.approved_by else '',
                        arcname,
                    ])
        manifest_io = io.StringIO()
        csv.writer(manifest_io).writerows(manifest_rows)
        # UTF-8 BOM 없이 저장하면 Excel(특히 한글 Windows)이 CP949로 잘못 인식해
        # 한글이 깨져 보인다 — utf-8-sig로 BOM을 붙여줘야 Excel에서 바로 정상 표시된다.
        zf.writestr('manifest.csv', manifest_io.getvalue().encode('utf-8-sig'))

    if skipped:
        messages.warning(request, f'승인본이 없어 제외된 품목: {", ".join(skipped)}')

    buffer.seek(0)
    # 헤더는 ASCII만 허용되므로 파일명은 영문으로 구성 (내용물 파일명은 한글 유지).
    filename = f'packaging_final_files_{datetime.now().strftime("%Y%m%d_%H%M")}.zip'
    response = HttpResponse(buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
