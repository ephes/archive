# archive

Self-hosted archive for podcast episodes, articles, videos, and social links shared from iOS/macOS, with automatic summaries, tags, and public feeds.

Current shipped scope:

- Milestone 1: authenticated capture, public website, admin/editor UI
- Milestone 2: public RSS feed with fixed-size archive pagination and autodiscovery
- Milestone 3: background URL metadata extraction for title/source/author/publication time/media URL
- Milestone 4: background short summary, long summary, and tag generation with operator-editable values

Public endpoints:

- `/` week-based public archive overview
- `/items/<id>/` public item detail page
- `/feeds/rss.xml` canonical general RSS feed
- `/feeds/rss/page/<n>.xml` older feed pages when more than 50 eligible items exist

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

## Background processing

Metadata extraction plus summary/tag generation runs in a separate worker process:

```bash
just manage run_metadata_worker --once
```

Summary generation is asynchronous and does not block capture or immediate publication. Failed summary
jobs retry automatically with bounded backoff (5 minutes, 30 minutes, 2 hours) before remaining in a
failed state for operator review.
