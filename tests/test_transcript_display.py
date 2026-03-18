from archive.transcript_display import split_transcript_for_display


def test_split_transcript_for_display_respects_existing_paragraph_breaks() -> None:
    transcript = "First paragraph line one.\nline two.\n\nSecond paragraph."

    assert split_transcript_for_display(transcript) == [
        "First paragraph line one.\nline two.",
        "Second paragraph.",
    ]


def test_split_transcript_for_display_normalizes_crlf_paragraph_breaks() -> None:
    transcript = "First paragraph line one.\r\nline two.\r\n\r\nSecond paragraph."

    assert split_transcript_for_display(transcript) == [
        "First paragraph line one.\nline two.",
        "Second paragraph.",
    ]


def test_split_transcript_for_display_preserves_single_newlines() -> None:
    transcript = "Speaker one starts here.\nSpeaker two answers on the next line."

    assert split_transcript_for_display(transcript) == [
        "Speaker one starts here.\nSpeaker two answers on the next line.",
    ]


def test_split_transcript_for_display_splits_long_wall_of_text_without_mutating_text() -> None:
    transcript = (
        "The archive should present transcripts in readable blocks even when the source only "
        "stored one wall of text with no blank lines at all. This first section is deliberately "
        "long enough to exceed the display threshold after a few complete sentences. Readers "
        "should not have to scan an endless slab of words just to follow the discussion. The "
        "second section continues with enough material to trigger another display paragraph break "
        "while keeping the stored transcript untouched for search and editing semantics. This "
        "keeps the public item page readable without pretending the underlying transcript already "
        "had author-provided paragraph structure."
    )

    paragraphs = split_transcript_for_display(transcript)

    assert len(paragraphs) >= 2
    assert " ".join(paragraphs) == " ".join(transcript.split())
    assert all(paragraph.strip() for paragraph in paragraphs)
    assert paragraphs[0][-1] in ".!?"


def test_split_transcript_for_display_waits_for_sentence_boundary_after_target() -> None:
    transcript = (
        "The opening sentence establishes enough context for the transcript formatting helper to "
        "start accumulating a readable block. The second sentence adds more detail so the running "
        "length crosses the target while the third sentence is still in progress with another "
        "readable chunk for the page that should not be split in the middle of the thought, and "
        "it keeps going with more detail so the helper must wait for a real sentence ending before "
        "splitting. The final sentence gives the helper a real boundary where the first paragraph "
        "may safely end."
    )

    paragraphs = split_transcript_for_display(transcript)

    assert len(paragraphs) >= 2
    assert paragraphs[0].endswith("splitting.")
    assert paragraphs[1].startswith("The final sentence")


def test_split_transcript_for_display_does_not_split_long_abbreviation_heavy_sentence() -> None:
    transcript = (
        "Dr. Example keeps describing a long scenario in the U.S. with repeated e.g. references "
        "that continue for quite a while without reaching a real sentence boundary, and A. B. test "
        "notes also appear in the same thought so the display helper should keep accumulating text "
        "instead of treating those abbreviations as paragraph endings while the explanation keeps "
        "going through more examples, more qualifiers, and more context until it finally reaches "
        "the actual sentence ending."
    )

    assert len(transcript) > 420
    assert len(transcript) < 640
    assert split_transcript_for_display(transcript) == [transcript]


def test_split_transcript_for_display_keeps_short_transcript_as_single_paragraph() -> None:
    transcript = "A short transcript should stay in a single paragraph."

    assert split_transcript_for_display(transcript) == [transcript]


def test_split_transcript_for_display_returns_empty_list_for_blank_input() -> None:
    assert split_transcript_for_display(" \n\n ") == []


def test_split_transcript_for_display_falls_back_for_punctuation_poor_input() -> None:
    transcript = " ".join(["word"] * 300)

    paragraphs = split_transcript_for_display(transcript)

    assert len(paragraphs) >= 2
    assert " ".join(paragraphs) == transcript
    assert all(paragraph.strip() for paragraph in paragraphs)
