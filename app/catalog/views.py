from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from .models import PackagingFile, Product


@login_required
def product_list(request):
    q = request.GET.get('q', '').strip()
    products = Product.objects.all()
    if q:
        products = products.filter(Q(name__icontains=q) | Q(code__icontains=q))
    rows = []
    for p in products:
        rows.append({
            'product': p,
            'final_file': p.current_final_file(),
            'version_count': p.files.count(),
            'open_request': p.has_open_request(),
        })
    return render(request, 'catalog/product_list.html', {'rows': rows, 'q': q})


@login_required
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    files = product.files.order_by('-version')
    return render(request, 'catalog/product_detail.html', {
        'product': product,
        'files': files,
        'open_request': product.has_open_request(),
    })
