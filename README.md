# archive

Self-hosted archive for podcast episodes, articles, videos, and social links shared from iOS/macOS, with automatic summaries, transcripts, and public feeds.

Current shipped scope:

- Milestone 1: authenticated capture, public website, admin/editor UI
- Milestone 2: public RSS feed with fixed-size archive pagination and autodiscovery

Public endpoints:

- `/` week-based public archive overview
- `/items/<id>/` public item detail page
- `/feeds/rss.xml` canonical general RSS feed
- `/feeds/rss/page/<n>.xml` older feed pages when more than 50 eligible items exist

## Development

```bash
just install
just check
just dev
```

## Configuration

The app reads its runtime configuration from environment variables.

Important values:

- `DJANGO_SETTINGS_MODULE` defaults to `config.settings.local`
- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DJANGO_DB_PATH`
- `ARCHIVE_API_TOKEN`
