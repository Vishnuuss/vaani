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


# --- guardrails now block rather than log ------------------------------------


class _FakeState:
    def __init__(self, **kw):
        self.must_end = kw.get("must_end", False)
        self.disqualified = kw.get("disqualified", False)
        self.next_step_agreed = kw.get("next_step_agreed", False)
        self.no_more_questions = kw.get("no_more_questions", False)


class _FakeInjector:
    def __init__(self, **kw):
        self.state = _FakeState(**kw)


def _filter(**state):
    from api.services.vaani.brain_processor import ReplyFilter

    f = ReplyFilter.__new__(ReplyFilter)
    f._injector = _FakeInjector(**state) if state else None
    f._sanitizer = ReplySanitizer()
    f._spoken = ""
    f._blocked = False
    f._said = []
    return f


def test_an_invented_price_is_replaced_before_it_is_spoken():
    from api.services.vaani import guardrails

    f = _filter()
    out = f._gate("ఇది 45000 రూపాయలు అవుతుంది")
    assert out == guardrails.SAFE_FALLBACK


def test_a_question_after_the_call_is_won_is_replaced():
    """The most common compliance failure: agreeing to stop, then asking anyway."""
    from api.services.vaani import guardrails

    f = _filter(next_step_agreed=True)
    out = f._gate("సరే అండి. మీ ఇంటి రూఫ్ ఎంత ఉంది?")
    assert out == guardrails.SAFE_CLOSE


def test_a_clean_reply_passes_the_gate_untouched():
    f = _filter()
    text = "సరేనండి. మీరు ఏ ప్రాంతంలో ఉన్నారు?"
    assert f._gate(text) == text


def test_once_blocked_nothing_further_is_spoken():
    f = _filter()
    f._gate("ఇది 45000 రూపాయలు")
    assert f._gate(" మరియు ఇంకా చాలా ఉంది") == ""


def test_markdown_is_advisory_not_blocking():
    """Cutting a caller off for a stray asterisk is worse than the asterisk."""
    f = _filter()
    text = "సరే **అండి**, చెప్పండి."
    assert f._gate(text) == text


def test_the_end_of_response_tail_cannot_bypass_the_gate():
    """Run 93 ended with "...మంచి రోజు సార్.all is ending: q" spoken aloud.

    The gate had already substituted a safe close, then the response-end flush
    pushed the model's remaining text past it. Once blocked, nothing more is
    spoken -- including the tail.
    """
    f = _filter()
    f._gate("ఇది 45000 రూపాయలు")          # trips the price rule, blocks
    assert f._gate("all is ending: q") == ""


# --- never say the same sentence twice ---------------------------------------


def _filter_with_history(said):
    from api.services.vaani.brain_processor import ReplyFilter

    f = ReplyFilter.__new__(ReplyFilter)
    f._injector = None
    f._sanitizer = ReplySanitizer()
    f._spoken = ""
    f._blocked = False
    f._said = list(said)
    return f


ASKED_ONCE = "సార్, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా వస్తుంది?"


def test_asking_the_same_question_again_becomes_a_repair_line():
    """Run 96 asked this four times word for word.

    The caller's verdict on that call was "you told me nothing". The reference
    agent never repeats -- it says it could not hear.
    """
    from api.services.vaani import guardrails

    f = _filter_with_history([ASKED_ONCE])
    assert f._gate(ASKED_ONCE) == guardrails.REPAIR_LINE


def test_a_reworded_repeat_is_still_a_repeat():
    """Run 96 varied the tail while asking the same thing."""
    from api.services.vaani import guardrails

    f = _filter_with_history([ASKED_ONCE])
    reworded = "సార్, మీ నెలవారీ బిల్లు సుమారు ఎంత రూపాయలుగా వస్తుంది?"
    assert f._gate(reworded) == guardrails.REPAIR_LINE


def test_a_genuinely_different_question_is_untouched():
    f = _filter_with_history([ASKED_ONCE])
    other = "సార్, మీరు ఏ నగరంలో ఉంటారు?"
    assert f._gate(other) == other


def test_a_short_acknowledgement_may_recur():
    """"సరే" is not a repeated sentence; it is normal speech."""
    f = _filter_with_history(["సరే."])
    assert f._gate("సరే.") == "సరే."


def test_nothing_follows_the_repair_line():
    from api.services.vaani import guardrails

    f = _filter_with_history([ASKED_ONCE])
    assert f._gate(ASKED_ONCE) == guardrails.REPAIR_LINE
    assert f._gate(" ఇంకా ఏదైనా?") == ""
