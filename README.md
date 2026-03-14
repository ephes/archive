# archive

Self-hosted archive for podcast episodes, articles, videos, and social links shared from iOS/macOS, with automatic summaries, tags, and public feeds.

Current shipped scope:

- Milestone 1: authenticated capture, public website, admin/editor UI
- Milestone 2: public RSS feed with fixed-size archive pagination and autodiscovery
- Milestone 3: background URL metadata extraction for title/source/author/publication time/media URL
- Milestone 4: background short summary, long summary, and tag generation with operator-editable values
- Milestone 5: background transcription for audio/video items with transcript-aware summary/tag refresh
- Milestone 6: background archival of eligible remote audio/video, stable local audio enclosure URLs, and a separate podcast-style RSS feed

Public endpoints:

- `/` week-based public archive overview
- `/items/<id>/` public item detail page
- `/feeds/rss.xml` canonical general RSS feed
- `/feeds/rss/page/<n>.xml` older feed pages when more than 50 eligible items exist
- `/feeds/podcast.xml` podcast-style feed for items with stable local audio enclosures
- `/feeds/podcast/page/<n>.xml` older podcast feed pages when more than 50 eligible items exist

## iOS Share Sheet Setup

On iOS, the standard Shortcuts **Get Contents of URL** action works for this workflow, including over the local network. We have not seen the macOS-only VPN/sandbox failure described below on iOS.

To share URLs from the iOS share sheet into the archive, create an Apple Shortcut:

### Steps

1. Open the **Shortcuts** app on iOS.
2. Create a new Shortcut.
3. Tap the **ⓘ** (info) button at the bottom of the editor and enable **Show in Share Sheet** — the toggle is not in the name dropdown menu on iOS. Under "Receives", select **URLs**.
4. Add the following actions:

**Action 1 — Get Contents of URL** (German: "Inhalte von URL abrufen")

- Search for "Inhalte von URL" in the action search — make sure you pick the one with the **green icon**, not "Inhalte der Webseite" (which is a different action).
- Tap the URL field to enter the API URL. If the field shows a variable pill (e.g. "Kurzbefehlseingabe"), tap the pill, then "Variable entfernen" to clear it first.
- **URL:** `https://archive.home.xn--wersdrfer-47a.de/api/items/`
- Tap the **⟩** expand arrow to reveal advanced settings.
- **Method:** POST
- **Headers:**
  - `Authorization`: `Bearer YOUR_API_TOKEN` — the value must include the `Bearer ` prefix (with a space), not just the token.
  - `Content-Type`: `application/json`
- **Request Body (JSON):**
  - Key: `url`, Value: tap the value field, then tap **Variable auswählen** and select **Kurzbefehlseingabe** (Shortcut Input).

**Action 2 — Show Notification** (optional, for feedback)

- Add "Show Notification" with text like `Archived!`

### iOS Pitfalls

- **"Show in Share Sheet" is hidden**: on iOS, it's under the ⓘ button, not in the shortcut name dropdown.
- **Wrong action**: "Inhalte der Webseite von … abrufen" (Get Contents of Web Page, Safari icon) is NOT the same as "Inhalte von URL abrufen" (Get Contents of URL, green icon). You need the green one.
- **Bearer prefix**: the Authorization header value must be `Bearer <token>`, not just the token. The API returns "Unauthorized" without the prefix.
- **Variable in URL field**: when you first add the action, it may auto-fill "Kurzbefehlseingabe" (Shortcut Input) in the URL field. Remove that variable and type the API URL instead. The Shortcut Input variable goes in the JSON body, not the URL.
- **URL field hard to edit**: to clear a variable from a field, tap the pill, then tap "Variable entfernen", then "Zurück". After that you can type in the field.

## macOS Share Sheet Setup

On macOS, the Shortcuts **Get Contents of URL** action is not reliable for Tailscale-only endpoints when the shortcut runs from the Share Sheet. In practice, the background Shortcuts runner appears to be sandboxed away from the VPN path, so requests either hang or silently fail.

As of 2026-03-10, we have not found an Apple-documented entitlement, system setting, configuration profile, or Tailscale setting that fixes this reliably. The recommended workaround is to use **Run Shell Script** with `curl`, which uses the normal system network stack and can reach Tailscale.

### Steps

1. Open the **Shortcuts** app on macOS.
2. Create a new Shortcut.
3. Click the name at the top and enable **Show in Share Sheet**. Under "Receives", select **URLs**.
4. Add the following actions:

**Action 1 — Get URLs from Input**

- Add "Get URLs from" and set input to **Shortcut Input**.
- If Shortcuts gives you duplicate URL lines in the shell action, keep this step and use `head -1` in the script below.
- If your browser shares a single plain URL cleanly, you can try skipping this step and passing **Shortcut Input** directly.

**Action 2 — Run Shell Script**

- Shell: **bash**
- Script:
  ```bash
  url=$(echo "URLS_VARIABLE" | head -1)
  curl -s -X POST https://archive.home.xn--wersdrfer-47a.de/api/items/ \
       -H "Authorization: Bearer YOUR_API_TOKEN" \
       -H "Content-Type: application/json" \
       -d "{\"url\": \"$url\"}"
  ```
  Replace `URLS_VARIABLE` with the magic variable from step 1: right-click inside the script text, choose **Insert Variable**, and select the **URLs** output from "Get URLs from". It will appear as a colored pill.
- In our testing, passing the URL via stdin or `$1` was less reliable than embedding the magic variable directly in the script text.
- The `head -1` is a workaround for Shortcuts sometimes coercing the shared input into a newline-separated list and repeating the same URL.
- Input: **Input**
- Pass Input: **to stdin**

**Action 3 — Show Alert** (optional, for feedback)

- Add "Show Alert" with text like `Archived!`

### Tips

- The API token is the `ARCHIVE_API_TOKEN` value from your deployment secrets.
- You can also pass optional fields in the JSON body: `title`, `notes`, `kind`, `audio_url`, `source`.
- For Castro specifically: Castro shares a URL, so the shortcut works as-is. The metadata extraction worker will enrich the item automatically.
- On macOS, share via **File > Share** in your browser, then double-click the shortcut and allow the permission prompt.

### Research Notes

- Apple documents **Get Contents of URL** as the standard API action, but we did not find Apple documentation covering this macOS Share Sheet plus VPN failure mode.
- Tailscale does not document a macOS per-app routing option that can force the Shortcuts background runner through the tunnel.
- If you are using the Mac App Store Tailscale client, it is still worth testing the Standalone Tailscale client, but we do not rely on that as a fix.
- The URL duplication looks like a Shortcuts Content Graph type-conversion issue rather than a `curl` or shell bug.
- A more robust future alternative is a local helper on `127.0.0.1` that Shortcuts can call directly, with the helper forwarding to the Tailscale-only API.

Reference docs:

- Apple Shortcuts: `Get Contents of URL`, input types, variables, and Content Graph
- Tailscale: macOS client variants, device-management settings, and Shortcuts integration

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
- `ARCHIVE_SUMMARY_API_KEY`
- `ARCHIVE_SUMMARY_API_BASE` defaults to `https://api.openai.com/v1`
- `ARCHIVE_SUMMARY_MODEL` defaults to `gpt-4o-mini`
- `ARCHIVE_TRANSCRIPTION_API_KEY` defaults to `ARCHIVE_SUMMARY_API_KEY`
- `ARCHIVE_TRANSCRIPTION_API_BASE` defaults to `ARCHIVE_SUMMARY_API_BASE`
- `ARCHIVE_TRANSCRIPTION_MODEL` defaults to `gpt-4o-mini-transcribe`
- `ARCHIVE_ARTICLE_AUDIO_API_KEY` defaults to `ARCHIVE_TRANSCRIPTION_API_KEY`
- `ARCHIVE_ARTICLE_AUDIO_API_BASE` defaults to `ARCHIVE_TRANSCRIPTION_API_BASE`
- `ARCHIVE_ARTICLE_AUDIO_MODEL` defaults to `tts-1`
- `ARCHIVE_ARTICLE_AUDIO_VOICE` defaults to empty and lets Voxhelm choose its default voice
- `ARCHIVE_ARTICLE_AUDIO_LANGUAGE` defaults to empty
- `ARCHIVE_ARTICLE_AUDIO_POLL_SECONDS` defaults to `30`
- `ARCHIVE_ARTICLE_AUDIO_MAX_BYTES` defaults to `52428800` (50 MiB)
- `ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES` defaults to `262144000` (250 MiB)
- `ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN` defaults to `ffmpeg`
- `ARCHIVE_MEDIA_STORAGE_BACKEND` defaults to local filesystem storage; set to `storages.backends.s3.S3Storage` for MinIO/S3-compatible object storage
- `ARCHIVE_MEDIA_STORAGE_LOCATION` defaults to `archive-media` under the project root for local filesystem storage
- `ARCHIVE_MEDIA_STORAGE_BUCKET_NAME` MinIO/S3 bucket for archived media objects
- `ARCHIVE_MEDIA_STORAGE_ENDPOINT_URL` MinIO/S3 endpoint URL
- `ARCHIVE_MEDIA_STORAGE_REGION_NAME` optional MinIO/S3 region
- `ARCHIVE_MEDIA_STORAGE_ACCESS_KEY_ID` MinIO/S3 access key
- `ARCHIVE_MEDIA_STORAGE_SECRET_ACCESS_KEY` MinIO/S3 secret key
- `ARCHIVE_MEDIA_STORAGE_ADDRESSING_STYLE` defaults to `path` for MinIO-friendly bucket addressing

## Background processing

Metadata extraction plus transcription plus summary/tag generation runs in a separate worker process:

```bash
just manage run_metadata_worker --once
```

Optional worker flags for the Milestone 6 slice:

- `--media-archive-timeout` controls the per-item remote media download timeout and is also used for
  video-to-audio extraction work

Summary generation is asynchronous and does not block capture or immediate publication. Audio/video
transcription is also asynchronous and writes transcript text back onto the item when a direct media source
can be fetched within the API size limit. Failed summary jobs retry automatically with bounded backoff
(5 minutes, 30 minutes, 2 hours) before remaining in a failed state for operator review. Article items can
also submit a Voxhelm batch `synthesize` job once a summary exists; Archive stores the private artifact
reference and exposes the finished audio through a public item-scoped proxy URL on the detail page. Podcast
episodes with a direct remote audio source are also archived asynchronously into the configured archive-media
storage backend and then served through a stable item-scoped Archive URL for playback and podcast enclosures.
Video items with a direct downloadable media URL (`.mp4`, `.m4v`, `.mov`, or `.webm`) are archived into the
same storage backend, then processed with `ffmpeg` to produce a stable local MP3 enclosure under
`/items/<id>/audio/`. Failed media archival jobs now retry with the same bounded backoff pattern as
summary generation
(5 minutes, 30 minutes, 2 hours) before remaining failed for operator review.
YouTube/Vimeo page URLs are not handled by this slice; the current implementation only supports direct
downloadable media files for video-derived local audio.

Operator note:

- the worker host must have `ffmpeg` installed for video-derived local audio extraction

Migration note:

- the Milestone 6 schema migrations re-queue existing eligible podcast/audio items, and later eligible
  direct-video items, by setting `media_archive_status` back to `pending` when they do not have a stable
  local audio copy yet
