import openpyxl
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ProductMasterImportForm
from .models import PackagingFile, Product


@login_required
def product_list(request):
    q = request.GET.get('q', '').strip()
    products = Product.objects.filter(is_active=True)
    if q:
        products = products.filter(Q(name__icontains=q) | Q(code__icontains=q))
    rows = []
    for p in products:
        rows.append({
            'product': p,
            'final_file': p.current_final_file(),
            'version_count': p.files.filter(is_active=True).count(),
            'open_request': p.has_open_request(),
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

            created = updated = skipped = 0
            for row in rows[1:]:  # 첫 행은 헤더로 간주하고 건너뜀
                if not row or len(row) < 2:
                    skipped += 1
                    continue
                code = str(row[0]).strip() if row[0] else ''
                name = str(row[1]).strip() if row[1] else ''
                if not code or not name:
                    skipped += 1
                    continue
                _, was_created = Product.objects.update_or_create(
                    code=code,
                    defaults={'name': name, 'category': category, 'product_line': product_line},
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

            summary = f'품목 마스터 등록 완료 — 신규 {created}건, 갱신 {updated}건'
            if skipped:
                summary += f', 건너뜀 {skipped}건(품목코드·품목명 누락)'
            messages.success(request, summary)
            return redirect('catalog:product_list')
    else:
        form = ProductMasterImportForm()
    return render(request, 'catalog/import_master.html', {'form': form})
