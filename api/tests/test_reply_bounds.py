"""Tests for the conversational reply bounds.

Both rules here exist because a plausible-looking value took the agent down in
testing, and neither failure was visible from the code alone.
"""

from __future__ import annotations

from api.services.vaani.reply_bounds import (
    MAX_REPLY_TOKENS,
    MAX_STOP_SEQUENCES,
    REPLY_STOP_SEQUENCES,
    conversational_extra,
)

# Measured against Groq on 2026-08-27, reasoning_effort=low. The cap is shared
# between thinking and speaking, so it must clear both.
WORST_MEASURED_REASONING_TOKENS = 152
WORST_MEASURED_CONTENT_TOKENS = 44


def test_stop_list_is_within_the_provider_limit():
    """Groq: "'stop' : maximum number of items is 4" -- a 400 on EVERY call."""
    assert len(REPLY_STOP_SEQUENCES) <= MAX_STOP_SEQUENCES


def test_no_bare_mode_stop():
    """A bare `MODE:` matches the protocol's own leading header at position 0.

    Measured: the completion comes back empty with finish_reason "stop", so
    nothing upstream flags it and the caller simply hears silence -- on every
    turn where the model obeys the format it was given.
    """
    assert "MODE:" not in REPLY_STOP_SEQUENCES
    for marker in REPLY_STOP_SEQUENCES:
        assert marker.startswith("\n"), f"{marker!r} can match at position 0"


def test_cap_leaves_room_to_think_and_still_speak():
    """The budget covers reasoning too.

    At 80 tokens the three hardest turns -- "what do you do", an angry caller,
    and a price objection -- spent 76-78 tokens reasoning and returned EMPTY
    replies. Those are exactly the turns the objection playbooks exist for.
    """
    assert MAX_REPLY_TOKENS > (
        WORST_MEASURED_REASONING_TOKENS + WORST_MEASURED_CONTENT_TOKENS
    )


def test_conversational_extra_adds_the_stops():
    out = conversational_extra({"reasoning_effort": "low"})
    assert out["reasoning_effort"] == "low", "existing keys must survive"
    assert set(REPLY_STOP_SEQUENCES) <= set(out["stop"])


def test_conversational_extra_does_not_mutate_its_input():
    """One settings object is reused across every service built in a run."""
    original = {"reasoning_effort": "low"}
    conversational_extra(original)
    assert original == {"reasoning_effort": "low"}


def test_a_caller_supplied_stop_list_is_never_overflowed():
    """Truncating costs one early stop; a 400 costs every call on the workflow."""
    out = conversational_extra({"stop": ["\nA:", "\nB:", "\nC:", "\nD:"]})
    assert len(out["stop"]) <= MAX_STOP_SEQUENCES


def test_existing_stops_are_not_duplicated():
    out = conversational_extra({"stop": ["\nMODE:"]})
    assert out["stop"].count("\nMODE:") == 1
