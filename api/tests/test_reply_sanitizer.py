"""Tests for the streaming reply sanitizer.

The anchor case is the real one: the exact 272-character blob run 12 spoke to a
caller on 2026-08-27, fed one character at a time the way it actually arrived.
"""

from __future__ import annotations

import pytest

from api.services.vaani.reply_sanitizer import ReplySanitizer

# Verbatim from run 12 (WR-TEL-OUT-07584411), rtf-bot-text turn 4.
RUN_12_BLOB = (
    "మీ ఇల్లు, అపార్ట్‌మెంట్ లేదా కమర్షియల్ ఏది?"
    "మీకు మీ స్వంత రూఫ్ స్పేస్ ఉంది కదా?"
    "My name is Rani."
    "సరే రాణి అండి, మా వేరిఫైడ్ వెండర్ మీకు సైట్ అసెస్‌మెంట్ కోసం కాల్ చేయవచ్చా?"
    "We have all info, need agreement."
    "MODE: CLOSE\n\n"
    "అవును, రాణి అండి, మీకు ఈ వారంలో ఏ రోజు సౌకర్యంగా ఉంటుంది?"
)


def run(fragments) -> tuple[str, ReplySanitizer]:
    """Stream fragments through a sanitizer and return the spoken text."""
    s = ReplySanitizer()
    out = "".join(s.feed(f) for f in fragments)
    return out + s.finish(), s


def char_by_char(text: str):
    return list(text)


# --- the real failure -------------------------------------------------------


def test_run_12_blob_never_speaks_the_control_token():
    spoken, s = run(char_by_char(RUN_12_BLOB))
    assert "MODE" not in spoken.upper()
    assert s.mode == "CLOSE", "the mode must still be captured, just not spoken"


def test_run_12_blob_does_not_speak_the_callers_invented_line():
    spoken, _ = run(char_by_char(RUN_12_BLOB))
    assert "My name is Rani" not in spoken


def test_run_12_blob_does_not_speak_the_internal_note():
    spoken, _ = run(char_by_char(RUN_12_BLOB))
    assert "need agreement" not in spoken


def test_run_12_blob_keeps_the_first_real_question():
    spoken, _ = run(char_by_char(RUN_12_BLOB))
    assert "మీ ఇల్లు" in spoken


# --- MODE, wherever it lands ------------------------------------------------


def test_leading_mode_line_is_stripped():
    spoken, s = run(["MODE: ASK\n", "సరే అండి."])
    assert spoken.strip() == "సరే అండి."
    assert s.mode == "ASK"


def test_mode_mid_reply_is_stripped():
    """The old filter only looked at the first line. This is run 12's hole."""
    spoken, s = run(["సరే అండి.", "MODE: CLOSE", "\n"])
    assert "MODE" not in spoken.upper()
    assert s.mode == "CLOSE"


def test_mode_with_space_before_colon_is_stripped():
    """`startswith("MODE:")` missed `MODE : END` silently."""
    spoken, s = run(["MODE : END\n", "ధన్యవాదాలు."])
    assert "MODE" not in spoken.upper()
    assert s.mode == "END"


def test_mode_end_is_reported_so_the_call_can_hang_up():
    _, s = run(["MODE: END\n", "ధన్యవాదాలు, మంచి రోజు!"])
    assert s.mode == "END"


def test_mode_split_across_streaming_fragments():
    """The LLM streams, so the marker arrives in pieces."""
    spoken, s = run(["సరే.", "\nMO", "DE", ": ", "CLO", "SE", "\n"])
    assert "MODE" not in spoken.upper()
    assert s.mode == "CLOSE"


# --- invented dialogue ------------------------------------------------------


@pytest.mark.parametrize("label", ["CUSTOMER", "Caller", "User", "Agent", "WRONG"])
def test_a_speaker_label_truncates_the_rest(label):
    spoken, _ = run([f"సరే అండి.\n{label}: ఏమిటి?\nInvented reply."])
    assert spoken.strip() == "సరే అండి."


def test_nothing_after_a_speaker_label_is_ever_released():
    s = ReplySanitizer()
    first = s.feed("మీ బిల్లు ఎంత?\nCUSTOMER: ")
    rest = s.feed("రెండు వేలు.") + s.finish()
    assert "CUSTOMER" not in first
    assert rest == "", "the sanitizer must stay closed once it has truncated"


# --- nothing is swallowed ---------------------------------------------------


def test_a_short_reply_with_no_newline_survives():
    """The old filter returned early under 40 chars and never flushed."""
    spoken, _ = run(["సరే."])
    assert spoken == "సరే."


def test_a_reply_shorter_than_the_holdback_survives():
    spoken, _ = run(["ok"])
    assert spoken == "ok"


def test_a_plain_reply_passes_through_unchanged():
    text = "సరేనండి. మీరు ఉంటున్న ఇల్లు సొంతమా అండి, లేక rent కి ఉంటున్నారా?"
    spoken, s = run(char_by_char(text))
    assert spoken == text
    assert s.mode is None


def test_streaming_and_whole_text_agree():
    """Fragment boundaries must not change the result."""
    whole, _ = run([RUN_12_BLOB])
    streamed, _ = run(char_by_char(RUN_12_BLOB))
    assert whole == streamed


# --- content that merely looks like a marker --------------------------------


def test_a_colon_in_ordinary_speech_is_not_a_speaker_label():
    text = "సమయం: రేపు ఉదయం పది గంటలకు."
    spoken, _ = run(char_by_char(text))
    assert spoken == text
