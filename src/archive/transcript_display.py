from __future__ import annotations

import re

PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n+")
TARGET_PARAGRAPH_CHARS = 420
MAX_PARAGRAPH_CHARS = 640
COMMON_ABBREVIATIONS = {
    "dr.",
    "mr.",
    "mrs.",
    "ms.",
    "prof.",
    "sr.",
    "jr.",
    "e.g.",
    "i.e.",
    "etc.",
    "vs.",
    "z.b.",
}
DOTTED_ABBREVIATION_RE = re.compile(r"(?i)^(?:[a-z]\.){2,}$")
INITIAL_RE = re.compile(r"(?i)^[a-z]\.$")


def split_transcript_for_display(transcript: str) -> list[str]:
    normalized = transcript.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    if PARAGRAPH_BREAK_RE.search(normalized):
        return [
            paragraph.strip()
            for paragraph in PARAGRAPH_BREAK_RE.split(normalized)
            if paragraph.strip()
        ]
    if "\n" in normalized:
        return [normalized]
    return _split_long_plain_text(normalized)


def _split_long_plain_text(text: str) -> list[str]:
    collapsed = " ".join(text.split())
    if not collapsed:
        return []

    paragraphs: list[str] = []
    current_words: list[str] = []
    current_chars = 0

    for word in collapsed.split():
        current_words.append(word)
        current_chars += len(word) + (1 if len(current_words) > 1 else 0)
        ends_sentence = _ends_sentence(word)

        if current_chars >= TARGET_PARAGRAPH_CHARS and ends_sentence:
            paragraphs.append(" ".join(current_words))
            current_words = []
            current_chars = 0
            continue

        if current_chars >= MAX_PARAGRAPH_CHARS:
            paragraphs.append(" ".join(current_words))
            current_words = []
            current_chars = 0

    if current_words:
        paragraphs.append(" ".join(current_words))

    return paragraphs


def _ends_sentence(word: str) -> bool:
    token = word.rstrip("\"')]}:;,").lower()
    if token.endswith(("!", "?")):
        return True
    if not token.endswith("."):
        return False
    if token in COMMON_ABBREVIATIONS:
        return False
    if INITIAL_RE.match(token):
        return False
    if DOTTED_ABBREVIATION_RE.match(token):
        return False
    return True
