from django.forms import ModelForm, Textarea

from archive.models import Item


class ItemForm(ModelForm):
    class Meta:
        model = Item
        fields = (
            "original_url",
            "title",
            "short_summary",
            "long_summary",
            "tags",
            "notes",
            "kind",
            "source",
            "audio_url",
            "is_public",
        )
        widgets = {
            "short_summary": Textarea(attrs={"rows": 3}),
            "long_summary": Textarea(attrs={"rows": 6}),
            "tags": Textarea(attrs={"rows": 3}),
            "notes": Textarea(attrs={"rows": 5}),
        }
