from django import forms

from .models import Product


class ProductMasterImportForm(forms.Form):
    file = forms.FileField(label='품목 리스트 엑셀 (.xlsx)')
    category = forms.ChoiceField(choices=Product.Category.choices, label='유형 (이 파일의 모든 품목에 적용)')
    product_line = forms.ChoiceField(choices=Product.ProductLine.choices, label='제품군 (이 파일의 모든 품목에 적용)')
