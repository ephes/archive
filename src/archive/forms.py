import hashlib
import json

from django.contrib.auth.forms import AuthenticationForm
from django.core import signing
from django.core.exceptions import ValidationError
from django.forms import CharField, HiddenInput, ModelForm, Textarea

from archive.models import EnrichmentStatus, Item
from archive.services import apply_operator_kind_override, normalize_item_kind_dependent_statuses


class ArchiveAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {
                "autocomplete": "username",
                "autocapitalize": "none",
                "autocorrect": "off",
                "spellcheck": "false",
            }
        )
        self.fields["password"].widget.attrs.update(
            {
                "autocomplete": "current-password",
            }
        )


class ItemForm(ModelForm):
    class Meta:
        model = Item
        fields = (
            "original_url",
            "title",
            "short_summary",
            "long_summary",
            "transcript",
            "tags",
            "notes",
            "kind",
            "source",
            "audio_url",
            "media_url",
            "podcast_feed_policy",
            "is_public",
        )
        widgets = {
            "short_summary": Textarea(attrs={"rows": 3}),
            "long_summary": Textarea(attrs={"rows": 6}),
            "transcript": Textarea(attrs={"rows": 16}),
            "tags": Textarea(attrs={"rows": 3}),
            "notes": Textarea(attrs={"rows": 5}),
        }

    def save(self, commit: bool = True) -> Item:
        item = super().save(commit=False)
        is_new = item.pk is None

        if "short_summary" in self.changed_data:
            item.short_summary_generated = False
        if "long_summary" in self.changed_data:
            item.long_summary_generated = False
        if "tags" in self.changed_data:
            item.tags_generated = False
        if "transcript" in self.changed_data:
            item.transcript_generated = False
            item.transcript_status = EnrichmentStatus.COMPLETE
            item.transcript_error = ""
        if "kind" in self.changed_data:
            apply_operator_kind_override(item=item, kind=item.kind)
        if is_new or "kind" in self.changed_data or "audio_url" in self.changed_data:
            normalize_item_kind_dependent_statuses(item=item)

        if commit:
            item.save()
            self.save_m2m()
        return item


class ItemAdminForm(ItemForm):
    content_revision = CharField(required=False, widget=HiddenInput)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Cover all persisted state, including generated flags and worker leases.
        # Admin's POST transaction starts before loading this instance; SQLite's
        # configured IMMEDIATE mode holds the write reservation through save.
        state = [field.value_to_string(self.instance) for field in Item._meta.concrete_fields]
        digest = hashlib.sha256(json.dumps(state).encode()).hexdigest()
        self._revision = f"{self.instance.pk}:{digest}"
        if self.instance.pk:
            self.initial["content_revision"] = self._signer().sign(self._revision)

    @staticmethod
    def _signer():
        return signing.Signer(salt="archive.admin.item")

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            try:
                revision = self._signer().unsign(cleaned.get("content_revision", ""))
            except signing.BadSignature:
                revision = None
            if revision != self._revision:
                raise ValidationError(
                    "This item changed since you opened it. Your submitted values are "
                    "shown below and have not been saved. Copy your edits, reload the "
                    "item, and reapply them to the current content."
                )
        return cleaned
