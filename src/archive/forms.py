from django.forms import ModelForm, Textarea

from archive.models import Item


class ItemForm(ModelForm):
    class Meta:
        model = Item
        fields = ("original_url", "title", "notes", "kind", "source", "audio_url", "is_public")
        widgets = {
            "notes": Textarea(attrs={"rows": 5}),
        }
