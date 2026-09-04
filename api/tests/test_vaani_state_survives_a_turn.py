"""The brain must remember across a turn boundary, or nothing that counts works.

A phone call keeps ONE CallState for its whole length, so this never mattered
there. Text chat does not: `text_chat_runner` builds a fresh pipeline for every
message and restored only `messages`, `gathered_context` and `tool_state`. The
Vaani brain therefore began every turn at zero.

Two rules depend entirely on counting, and both were dead in text chat:

    MAX_ASKS_PER_FIELD   `ask_counts` was always {} -> a field was never
                         abandoned, so the same question came back forever
    _is_repeat           `asked` was always [] -> nothing to compare against,
                         so the repair line never fired

Probed live on 4 Sep: wf3 asked its question on five consecutive turns, wf6 on
three. Both are the client's "it is looping questions again and again ... it is
not remembering the answers".

The eval battery runs on text chat too, so its repetition failures have been
measuring this rather than the agent.
"""

from __future__ import annotations

from api.services.vaani.state import CallState


def _state() -> CallState:
    return CallState(
        required_fields=["monthly_bill", "customer_name"],
        questions={"monthly_bill": "బిల్లు ఎంత?", "customer_name": "మీ పేరు?"},
    )


def test_the_ask_budget_survives_a_rebuild():
    """The whole point: turn three must know what turns one and two asked."""
    first = _state()
    first.last_user_text = "ఏం బిల్లు అండి?"
    first.render(); first.commit_ask()
    first.render(); first.commit_ask()
    assert first.ask_counts.get("monthly_bill") == 2

    later = _state()                      # a fresh pipeline, as text chat builds
    later.restore(first.snapshot())
    assert later.ask_counts.get("monthly_bill") == 2
    assert "monthly_bill" not in later.still_need, (
        "the field was asked twice and must be abandoned, not asked again")


def test_what_the_caller_said_survives():
    first = _state()
    first.learn("monthly_bill", "3000")
    later = _state()
    later.restore(first.snapshot())
    assert later.known.get("monthly_bill") == "3000"
    assert "monthly_bill" not in later.still_need


def test_the_spoken_history_survives_so_repeats_can_be_seen():
    first = _state()
    first.asked.append("మీ నెల బిల్లు ఎంత వస్తుంది?")
    later = _state()
    later.restore(first.snapshot())
    assert later.asked == ["మీ నెల బిల్లు ఎంత వస్తుంది?"]


def test_the_end_of_a_call_survives():
    """A caller who asked to be left alone must not be re-asked next turn."""
    first = _state()
    first.must_end = True
    first.end_reason = "removal requested"
    first.no_more_questions = True
    later = _state()
    later.restore(first.snapshot())
    assert later.must_end and later.no_more_questions
    assert later.end_reason == "removal requested"


def test_a_snapshot_is_json_safe():
    import json
    st = _state()
    st.learn("monthly_bill", "3000")
    st.ask_counts["customer_name"] = 1
    st.asked.append("మీ పేరు?")
    json.dumps(st.snapshot())          # must not raise


def test_restore_ignores_rubbish():
    """The checkpoint is stored data. It is not trusted to be well formed."""
    st = _state()
    st.restore(None)
    st.restore({"not_a_field": 1, "known": {"monthly_bill": "3000"}})
    assert st.known == {"monthly_bill": "3000"}
    assert not hasattr(st, "not_a_field")


def test_an_empty_snapshot_restores_cleanly():
    st = _state()
    st.restore(_state().snapshot())
    assert st.ask_counts == {}
    assert st.still_need == ["monthly_bill", "customer_name"]


def test_configuration_is_not_persisted():
    """required_fields and questions come from the workflow every turn. Freezing
    them into a checkpoint would pin an agent to the version that wrote it."""
    snap = _state().snapshot()
    assert "required_fields" not in snap
    assert "questions" not in snap
