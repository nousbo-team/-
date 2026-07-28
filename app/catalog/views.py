from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

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
