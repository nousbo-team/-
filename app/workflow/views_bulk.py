import csv
import io
import re
import zipfile
from datetime import datetime

import openpyxl
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from catalog.models import PackagingFile, Product

PAIR_SUFFIX_RE = re.compile(r'(_ai|_jpg|_jpeg)$', re.IGNORECASE)


@login_required
def bulk_home(request):
    products = Product.objects.all().order_by('name')
    return render(request, 'workflow/bulk.html', {'products': products})


def _read_mapping(mapping_file):
    """엑셀 매핑표(파일명/품목코드/품목명/승인일/승인자/비고) → {파일명(확장자 제외): row dict}"""
    mapping = {}
    wb = openpyxl.load_workbook(mapping_file, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return mapping
    header = [str(h).strip() if h else '' for h in rows[0]]
    for row in rows[1:]:
        data = dict(zip(header, row))
        filename = str(data.get('파일명') or '').strip()
        if not filename:
            continue
        key = PAIR_SUFFIX_RE.sub('', filename.rsplit('.', 1)[0])
        mapping[key] = {
            'item_code': str(data.get('품목코드') or '').strip(),
            'product_name': str(data.get('품목명') or '').strip(),
            'approved_date': data.get('승인일'),
            'approver': str(data.get('승인자') or '').strip(),
            'note': str(data.get('비고') or '').strip(),
        }
    return mapping


def _resolve_product(base_key, map_row):
    """품목코드가 있으면 코드 기준으로 upsert(품목명 변경도 최신으로 갱신), 없으면
    기존 품목명으로만 매칭한다(매핑표 없이는 신규 품목을 만들지 않는다)."""
    item_code = map_row.get('item_code') if map_row else ''
    product_name = (map_row.get('product_name') if map_row else '') or base_key

    if item_code:
        product = Product.objects.filter(code=item_code).first()
        if product:
            if product_name and product.name != product_name:
                product.name = product_name
                product.save(update_fields=['name'])
            return product
        if map_row.get('product_name'):
            # 매핑표에 유형/제품군 정보가 없으므로 기본값으로 생성 — 필요 시 /admin에서 보정.
            return Product.objects.create(
                code=item_code, name=product_name,
                category=Product.Category.LABEL, product_line=Product.ProductLine.FERTILIZER,
            )
        return None

    product = Product.objects.filter(name=product_name).first()
    if not product:
        product = next((p for p in Product.objects.all() if p.name in base_key), None)
    return product


@login_required
def bulk_upload(request):
    if request.method != 'POST':
        return redirect('workflow:bulk')

    files = request.FILES.getlist('files')
    mapping_file = request.FILES.get('mapping_file')
    mapping = _read_mapping(mapping_file) if mapping_file else {}

    groups = {}
    for f in files:
        stem, _, ext = f.name.rpartition('.')
        base_key = PAIR_SUFFIX_RE.sub('', stem or f.name)
        groups.setdefault(base_key, {})[ext.lower()] = f

    registered, unmatched = [], []
    for base_key, pair in groups.items():
        ai_file = pair.get('ai')
        jpg_file = pair.get('jpg') or pair.get('jpeg')
        if not ai_file or not jpg_file:
            unmatched.append(f'{base_key} (AI/JPG 짝이 맞지 않음)')
            continue

        map_row = mapping.get(base_key)
        product = _resolve_product(base_key, map_row)
        if not product:
            unmatched.append(f'{base_key} (품목 미매칭 — 엑셀 매핑표에 품목코드·품목명을 지정하거나 /admin에서 수동 등록하세요)')
            continue

        pkg = PackagingFile.objects.create(
            product=product, ai_file=ai_file, jpg_file=jpg_file, uploaded_by=request.user,
            note=(map_row['note'] if map_row else '') or '일괄 이관 업로드',
        )
        if map_row and map_row.get('approver'):
            approved_at = None
            raw_date = map_row.get('approved_date')
            if isinstance(raw_date, datetime):
                approved_at = raw_date
            elif raw_date:
                try:
                    approved_at = datetime.strptime(str(raw_date), '%Y-%m-%d')
                except ValueError:
                    approved_at = None
            pkg.approve(request.user)
            if approved_at:
                if timezone.is_naive(approved_at):
                    approved_at = timezone.make_aware(approved_at)
                pkg.approved_at = approved_at
            pkg.note = f"{pkg.note} · 원 승인자: {map_row['approver']}"
            pkg.save(update_fields=['approved_at', 'note'])
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
            for field, label in ((final_file.ai_file, 'ai'), (final_file.jpg_file, 'jpg')):
                if field:
                    arcname = f'{folder}/v{final_file.version}_{folder}.{label}'
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
