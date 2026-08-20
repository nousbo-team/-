import openpyxl
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from workflow.models import ReorderRequest

from .forms import ProductMasterImportForm
from .models import PackagingFile, Product


@login_required
def product_list(request):
    """품목 하나마다 current_final_file()/has_open_request() 등을 호출하면 품목 수만큼
    쿼리가 늘어난다 — 품목이 수백 건 규모가 되면 요청 하나가 원격 DB 왕복을 수백 번
    반복하게 되어 워커 타임아웃(502/500)으로 이어진다. 파일·재발주 건을 각각 한 번씩만
    조회해 품목별로 파이썬에서 묶는다."""
    q = request.GET.get('q', '').strip()
    products = Product.objects.filter(is_active=True)
    if q:
        products = products.filter(Q(name__icontains=q) | Q(code__icontains=q))
    products = list(products)
    product_ids = [p.pk for p in products]

    files_by_product = {}
    for f in PackagingFile.objects.filter(product_id__in=product_ids, is_active=True).order_by('product_id', '-version'):
        files_by_product.setdefault(f.product_id, []).append(f)

    open_request_by_product = {}
    for r in (ReorderRequest.objects
              .filter(product_id__in=product_ids)
              .exclude(status__in=ReorderRequest.TERMINAL_STATUSES)
              .order_by('product_id', '-created_at')):
        open_request_by_product.setdefault(r.product_id, r)  # 품목별로 가장 최근 건만 남김

    rows = []
    for p in products:
        files = files_by_product.get(p.pk, [])
        final_file = next((f for f in files if f.status == PackagingFile.Status.FINAL_APPROVED), None)
        rows.append({
            'product': p,
            'final_file': final_file,
            'version_count': len(files),
            'open_request': open_request_by_product.get(p.pk),
        })
    return render(request, 'catalog/product_list.html', {'rows': rows, 'q': q})


@login_required
def product_detail(request, pk):
    # 목록/검색에서는 숨기지만, 기존 재발주 건 등에서 직접 들어오는 링크는 막지 않는다.
    product = get_object_or_404(Product, pk=pk)
    files = product.files.filter(is_active=True).order_by('-version')
    return render(request, 'catalog/product_detail.html', {
        'product': product,
        'files': files,
        'open_request': product.has_open_request(),
    })


@login_required
def import_master_list(request):
    """품목코드·품목명 두 열짜리 엑셀(ERP 품목 마스터 등)을 업로드해 품목을 일괄
    등록/갱신한다(관리자 전용). 파일 안의 모든 행에 화면에서 고른 유형·제품군을
    똑같이 적용한다 — 보통 엑셀 한 개가 같은 종류(예: 비료용 PP포대)의 리스트이기
    때문. 이미 있는 품목코드는 품목명·유형·제품군을 최신 값으로 갱신한다."""
    if not request.user.is_superuser:
        raise PermissionDenied('관리자 계정만 사용할 수 있습니다.')

    if request.method == 'POST':
        form = ProductMasterImportForm(request.POST, request.FILES)
        if form.is_valid():
            wb = openpyxl.load_workbook(form.cleaned_data['file'], read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            category = form.cleaned_data['category']
            product_line = form.cleaned_data['product_line']

            entries = {}  # code -> name, 뒤에 나온 행이 이기도록(중복 코드 대비) dict 사용
            skipped = 0
            for row in rows[1:]:  # 첫 행은 헤더로 간주하고 건너뜀
                if not row or len(row) < 2:
                    skipped += 1
                    continue
                code = str(row[0]).strip() if row[0] else ''
                name = str(row[1]).strip() if row[1] else ''
                if not code or not name:
                    skipped += 1
                    continue
                entries[code] = name

            # 행마다 개별 쿼리를 날리면(update_or_create) 원격 DB 왕복 지연이 누적돼
            # 수백 건 규모에서 gunicorn 워커 타임아웃(502)으로 이어진다 — 기존 품목
            # 조회 1번 + bulk_create/bulk_update 각 1번, 총 몇 번의 쿼리로 처리한다.
            existing = {p.code: p for p in Product.objects.filter(code__in=entries.keys())}
            to_create, to_update = [], []
            for code, name in entries.items():
                if code in existing:
                    p = existing[code]
                    p.name = name
                    p.category = category
                    p.product_line = product_line
                    to_update.append(p)
                else:
                    to_create.append(Product(code=code, name=name, category=category, product_line=product_line))

            with transaction.atomic():
                if to_create:
                    Product.objects.bulk_create(to_create, batch_size=200)
                if to_update:
                    Product.objects.bulk_update(to_update, ['name', 'category', 'product_line'], batch_size=200)

            created, updated = len(to_create), len(to_update)
            summary = f'품목 마스터 등록 완료 — 신규 {created}건, 갱신 {updated}건'
            if skipped:
                summary += f', 건너뜀 {skipped}건(품목코드·품목명 누락)'
            messages.success(request, summary)
            return redirect('catalog:product_list')
    else:
        form = ProductMasterImportForm()
    return render(request, 'catalog/import_master.html', {'form': form})
