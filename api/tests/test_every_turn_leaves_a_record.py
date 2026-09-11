"""Every turn writes down why it did what it did.

On 11 Sep two diagnoses were reached by reading transcripts and inferring the
state behind them, and both were wrong:

  - Run 870's repeated question was blamed on MAX_ASKS_PER_FIELD. The ask budget
    was not the cause; it is refunded and re-issued, and a first test PASSED
    WITHOUT THE FIX because in simulation the cap bites and in production it
    does not.
  - The 0.85s endpoint floor was blamed on `endpoint_min_secs`. Those timers are
    never read on these agents -- a different stop strategy is configured, and
    the floor comes from a fixed timer that ignores them.

Both mistakes have the same shape: a transcript shows what was SAID and nothing
shows what was KNOWN. So each turn now emits its counters.

Kept small deliberately. This runs on every turn of every call, and a log that
costs latency is a log that gets turned off.
"""

from __future__ import annotations

from api.services.vaani.state import CallState


def _state() -> CallState:
    s = CallState()
    s.required_fields = ["house_ownership", "solar_planning"]
    s.questions = {f: f"tell me your {f}" for f in s.required_fields}
    return s


def test_the_record_carries_the_counters_a_diagnosis_needs():
    s = _state()
    s.pending_ask = "house_ownership"
    s.commit_ask()
    s.note_answer_to_last_ask("ఆ సంద మేమ్")

    log = s.turn_log()
    assert log["asked_counts"] == {"house_ownership": 1}
    assert log["answer_counts"] == {"house_ownership": 1}, (
        "the counter that actually bounds the loop must be visible")
    assert log["heard"] == {"house_ownership": ["ఆ సంద మేమ్"]}, (
        "what he said is the evidence. Run 870 was only understood by reading "
        "his exact garbled words back")
    assert log["last_asked"] == "house_ownership"
    assert "still_need" in log and "known" in log


def test_it_is_json_safe():
    """It is written with json.dumps on a live call. Sets are not."""
    import json

    s = _state()
    s.pending_ask = "house_ownership"
    s.commit_ask()
    s.note_answer_to_last_ask("సొంత ఇల్లు")
    json.dumps(s.turn_log(), ensure_ascii=False)


def test_a_retired_field_is_visible_as_retired():
    """The question a diagnosis always asks: why did it stop asking that?"""
    s = _state()
    for text in ("ఆ సంద మేమ్", "పాయి సంతీల్"):
        s.pending_ask = "house_ownership"
        s.commit_ask()
        s.note_user_said(text)
        s.note_answer_to_last_ask(text)
        s.end_user_turn()          # what ReplyFilter does once a reply is out
        s.answered_pending = set()

    log = s.turn_log()
    assert log["answer_counts"]["house_ownership"] == 2
    assert "house_ownership" not in log["still_need"]
    assert not log["known"].get("house_ownership"), (
        "retired because he answered twice, NOT because we understood him -- "
        "and the record has to show the difference")
