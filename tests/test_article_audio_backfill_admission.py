"""Anonymous requests must not drive repeated authenticated upstream copies.

The public article-audio route copies a missing stored copy from Voxhelm on demand.
Each copy holds a server thread for up to its 30 s budget and downloads up to the
configured byte limit with the API key, so it is admitted at most once per item
artifact per window; other requests in that window get a 503 with Retry-After.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from django.core.cache import cache
from django.urls import reverse

from archive import views
from archive.models import EnrichmentStatus, Item, ItemKind

PAYLOAD = b"\xff\xfb" + bytes(range(256)) * 40
pytestmark = pytest.mark.django_db


def make_article(path="/speech.mp3", url="https://example.com/article"):
    return Item.objects.create(
        original_url=url,
        title="Article",
        kind=ItemKind.ARTICLE,
        short_summary="A substantial essay.",
        long_summary="A coherent argument. " * 40,
        summary_status=EnrichmentStatus.COMPLETE,
        is_public=True,
        article_audio_artifact_path=path,
        article_audio_generated=True,
        article_audio_status=EnrichmentStatus.COMPLETE,
    )


@pytest.fixture
def upstream(settings):
    state = {"requests": [], "fail": True}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            if state["fail"]:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = f"http://127.0.0.1:{server.server_port}/v1"
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "fake-key"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def audio_url(item):
    return reverse("archive:item-article-audio", kwargs={"pk": item.pk})


def body(response):
    try:
        return b"".join(response.streaming_content) if response.streaming else response.content
    finally:
        response.close()


def test_failed_public_copy_is_not_retried_by_every_request(client, upstream):
    article = make_article()
    first = client.get(audio_url(article))
    assert first.status_code == 503
    assert first["Retry-After"] == "60"
    assert len(upstream["requests"]) == 1

    for method in (client.get, client.head, client.get):
        response = method(audio_url(article))
        assert response.status_code == 503
        assert response["Retry-After"] == "60"
        assert response["Cache-Control"] == "no-store"
    assert len(upstream["requests"]) == 1

    # Once the window has passed, the next request tries again and can succeed.
    cache.clear()
    upstream["fail"] = False
    response = client.get(audio_url(article))
    assert response.status_code == 200
    assert body(response) == PAYLOAD
    assert len(upstream["requests"]) == 2
    # The stored copy is served without contacting upstream again.
    assert body(client.get(audio_url(article))) == PAYLOAD
    assert len(upstream["requests"]) == 2


def test_concurrent_request_during_a_copy_does_not_start_another(client, upstream, monkeypatch):
    article = make_article()
    upstream["fail"] = False
    nested = []
    original = views.store_generated_article_audio

    def store_while_another_request_arrives(*, item):
        nested.append(client.get(audio_url(article)))
        original(item=item)

    monkeypatch.setattr(views, "store_generated_article_audio", store_while_another_request_arrives)
    response = client.get(audio_url(article))
    assert response.status_code == 200
    assert body(response) == PAYLOAD
    assert [r.status_code for r in nested] == [503]
    assert nested[0]["Retry-After"] == "60"
    assert len(upstream["requests"]) == 1


def test_window_is_per_item_and_per_artifact(client, upstream):
    first = make_article()
    second = make_article(url="https://example.com/other")
    assert client.get(audio_url(first)).status_code == 503
    assert client.get(audio_url(second)).status_code == 503
    assert len(upstream["requests"]) == 2

    # A new synthesis artifact for the same item is a different copy.
    Item.objects.filter(pk=first.pk).update(article_audio_artifact_path="/speech-2.mp3")
    upstream["fail"] = False
    response = client.get(audio_url(first))
    assert response.status_code == 200
    assert body(response) == PAYLOAD
    assert upstream["requests"][-1].endswith("/speech-2.mp3")
    assert len(upstream["requests"]) == 3


def test_deferred_request_for_unpublished_item_is_not_found(client, upstream):
    article = make_article()
    assert client.get(audio_url(article)).status_code == 503
    Item.objects.filter(pk=article.pk).update(is_public=False)
    response = client.get(audio_url(article))
    assert response.status_code == 404
    assert response["Cache-Control"] == "no-store"
    assert len(upstream["requests"]) == 1
