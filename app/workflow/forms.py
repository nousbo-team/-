from django import forms

from catalog.models import Product

from .models import ReorderRequest


class ProductChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f'[{obj.code}] {obj.name}'


class NewRequestForm(forms.Form):
    product = ProductChoiceField(queryset=Product.objects.all(), label='품목')
    reason = forms.ChoiceField(choices=ReorderRequest.Reason.choices, label='사유')


class DesignUploadForm(forms.Form):
    ai_file = forms.FileField(label='AI 파일')
    jpg_file = forms.ImageField(label='JPG 파일')
    note = forms.CharField(label='수정 내용', required=False, widget=forms.Textarea)


# 일괄 업로드는 다중 파일 처리가 필요해 Form을 거치지 않고 views_bulk.py에서
# request.FILES.getlist()로 직접 처리한다 (ClearableFileInput은 다중 업로드 미지원).
