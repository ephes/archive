from __future__ import annotations

import sys

from archive import metadata
from archive.metadata import extract_metadata_from_html

BASE_URL = "https://example.com/post"


def _count_metadata_lines(html: str) -> int:
    path = metadata.__file__
    count = 0

    def local_trace(frame, event, arg):
        nonlocal count
        if event == "line":
            count += 1
        return local_trace

    def global_trace(frame, event, arg):
        return local_trace if frame.f_code.co_filename == path else None

    previous = sys.gettrace()
    sys.settrace(global_trace)
    try:
        extract_metadata_from_html(html, base_url=BASE_URL)
    finally:
        sys.settrace(previous)
    return count


def _malformed_media_page(size: int) -> str:
    return "<html><body>" + "<audio>" * size + "</video>" * size + "</body></html>"


def test_unmatched_media_close_tags_cost_linear_work() -> None:
    small = _count_metadata_lines(_malformed_media_page(200))
    large = _count_metadata_lines(_malformed_media_page(400))

    # Linear work doubles; the old per-close scan of every open context quadrupled.
    assert large < small * 3


def test_matched_out_of_order_media_close_tags_cost_linear_work() -> None:
    def page(size: int) -> str:
        return (
            "<html><body>"
            + "<video>" * size
            + "<audio>" * size
            + "</video>" * size
            + "</body></html>"
        )

    small = _count_metadata_lines(page(200))
    large = _count_metadata_lines(page(400))

    assert large < small * 3


def test_source_parent_follows_the_innermost_open_media_context() -> None:
    html = """
    <html><body>
      <audio><video><audio></video><source src="/a.mp3"></audio><source src="/b.mp3">
      </audio><source src="/ignored.mp4">
      <video><audio></video><source src="/c.mp3"></audio>
      <audio><video></audio><source src="/d.mp4"></video>
      <video></audio><source src="/e.mp4"></video>
    </body></html>
    """
    result = extract_metadata_from_html(html, base_url=BASE_URL)

    assert [(c.url, c.candidate_type) for c in result.media_candidates] == [
        ("https://example.com/a.mp3", "audio"),
        ("https://example.com/b.mp3", "audio"),
        ("https://example.com/c.mp3", "audio"),
        ("https://example.com/d.mp4", "video"),
        ("https://example.com/e.mp4", "video"),
    ]
