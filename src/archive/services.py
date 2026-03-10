from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

from django.utils import timezone

from archive.models import ItemKind

VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")


def infer_kind(url: str, explicit_kind: str = "", audio_url: str = "") -> str:
    if explicit_kind in ItemKind.values:
        return explicit_kind
    if audio_url:
        return "podcast_episode"

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if host in VIDEO_HOSTS:
        return "video"
    if path.endswith(AUDIO_SUFFIXES):
        return "podcast_episode"
    return "link"


@dataclass(frozen=True)
class WeekPage:
    token: str
    year: int
    week: int
    starts_on: date
    ends_on: date

    @property
    def label(self) -> str:
        return f"Week {self.week}, {self.year}"


def to_week_page(value: datetime) -> WeekPage:
    local_value = timezone.localtime(value)
    iso_year, iso_week, _ = local_value.isocalendar()
    starts_on = date.fromisocalendar(iso_year, iso_week, 1)
    ends_on = date.fromisocalendar(iso_year, iso_week, 7)
    return WeekPage(
        token=f"{iso_year}-W{iso_week:02d}",
        year=iso_year,
        week=iso_week,
        starts_on=starts_on,
        ends_on=ends_on,
    )


def week_bounds(page: WeekPage) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    starts_at = timezone.make_aware(datetime.combine(page.starts_on, time.min), tz)
    ends_at = timezone.make_aware(datetime.combine(page.ends_on + timedelta(days=1), time.min), tz)
    return starts_at, ends_at
