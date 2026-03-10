# archive

Self-hosted archive for podcast episodes, articles, videos, and social links shared from iOS/macOS, with automatic summaries, transcripts, and public feeds.

Current shipped scope:

- Milestone 1: authenticated capture, public website, admin/editor UI
- Milestone 2: public RSS feed with fixed-size archive pagination and autodiscovery
- Milestone 3: background URL metadata extraction for title/source/author/publication time/media URL

Public endpoints:

- `/` week-based public archive overview
- `/items/<id>/` public item detail page
- `/feeds/rss.xml` canonical general RSS feed
- `/feeds/rss/page/<n>.xml` older feed pages when more than 50 eligible items exist

## iOS / macOS Share Sheet Setup

To share URLs from the iOS or macOS share sheet into the archive, create an Apple Shortcut:

### Steps

1. Open the **Shortcuts** app on iOS (or macOS).
2. Create a new Shortcut.
3. Tap the name at the top and enable **Show in Share Sheet**. Under "Receives", select **URLs** (and optionally **Text**).
4. Add the following actions:

**Action 1 — Get URLs from Input**

- Add "Get URLs from" and set input to **Shortcut Input**.

**Action 2 — Get Contents of URL** (this is the API call)

- **URL:** `https://archive.home.xn--wersdrfer-47a.de/api/items/`
- **Method:** POST
- **Headers:**
  - `Authorization`: `Bearer YOUR_API_TOKEN`
  - `Content-Type`: `application/json`
- **Request Body (JSON):**
  - `url`: the URL from step 1

**Action 3 — Show Notification** (optional, for feedback)

- Add "Show Notification" with text like `Archived!`

### Tips

- The API token is the `ARCHIVE_API_TOKEN` value from your deployment secrets.
- You can also pass optional fields in the JSON body: `title`, `notes`, `kind`, `audio_url`, `source`.
- For Castro specifically: Castro shares a URL, so the shortcut works as-is. The metadata extraction worker will enrich the item automatically.
- The shortcut works identically on macOS via the share menu.

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

## Background processing

Metadata extraction runs in a separate worker process:

```bash
just manage run_metadata_worker --once
```
