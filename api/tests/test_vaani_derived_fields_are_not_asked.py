"""A variable with no written `ask` is DERIVED, and must never be a question.

The client, 4 Sep, about every agent except MB Solar: "it is not remembering the
answers and it is looping questions again and again ... it is not responding to
my doubts ... skipping question".

Probed the same day against the live server:

    wf3 (loan)     asked "ఏ loan అండి?" five turns running, never acknowledged
                   the caller's name, deflected every question
    wf6 (property) asked "మీరు ఇప్పుడు property కొనాలని చూస్తున్నారా అండి?"
                   three times, progressing to budget and then REVERTING

MB Solar, probed identically, progressed cleanly. The difference is in the data:

    wf2  property_type, monthly_bill, location, roof_available,
         customer_name, assessment_agreed          -- all six carry an `ask`
    wf3  loan_required, loan_type                  + do_not_call, summary
    wf6  property_interest, property_type, budget,
         location, timeline                        + lead_score, do_not_call,
                                                     summary

`do_not_call`, `summary` and `lead_score` are worked out BY the extractor from
the call. Nobody wrote a spoken question for them because nobody would ever say
one. They were reaching the qualification checklist regardless, and
`ask or name` then offered the model the literal word "summary" to ask a caller.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# run_pipeline imports the world; the function under test does not need it.
_SRC = (Path(__file__).resolve().parents[1]
        / "services" / "pipecat" / "run_pipeline.py").read_text(encoding="utf-8")


def _questions(variables) -> list[dict]:
    """Re-run build_vaani_brain's variable loop in isolation."""
    out = []
    for variable in variables:
        name = variable.get("name") or ""
        if not name:
            continue
        written = (variable.get("ask") or "")
        if not written.strip():
            continue
        out.append({"field": name, "ask": written})
    return out


WF3 = [
    {"name": "loan_required", "ask": "ఏ loan అండి?"},
    {"name": "loan_type", "ask": "ఏ loan అండి?"},
    {"name": "do_not_call", "ask": "", "prompt": "true if they asked not to be called"},
    {"name": "summary", "prompt": "one line summary of the call"},
]

WF6 = [
    {"name": "property_interest", "ask": "మీరు ఇప్పుడు property కొనాలని చూస్తున్నారా అండి?"},
    {"name": "budget", "ask": "మీ Budget ఎంత వరకు ఉందా అండి?"},
    {"name": "lead_score", "prompt": "1-10"},
    {"name": "do_not_call", "prompt": "true if they asked not to be called"},
    {"name": "summary", "prompt": "one line summary"},
]


def test_the_loop_in_run_pipeline_matches_this_helper():
    """If build_vaani_brain stops filtering, this file stops meaning anything."""
    assert 'written = (variable.get("ask")' in _SRC
    assert "if not written.strip():" in _SRC


@pytest.mark.parametrize("variables,derived", [
    (WF3, {"do_not_call", "summary"}),
    (WF6, {"lead_score", "do_not_call", "summary"}),
])
def test_derived_variables_never_become_questions(variables, derived):
    fields = {q["field"] for q in _questions(variables)}
    assert not (fields & derived), f"would ask the caller about {fields & derived}"


def test_the_real_questions_survive():
    assert {q["field"] for q in _questions(WF3)} == {"loan_required", "loan_type"}
    assert {q["field"] for q in _questions(WF6)} == {"property_interest", "budget"}


def test_no_question_is_ever_a_field_name():
    """The whole point. "summary" reaching a caller's ear is the bug."""
    for variables in (WF3, WF6):
        for q in _questions(variables):
            assert q["ask"] != q["field"]
            assert q["ask"].strip()


def test_mb_solar_is_unaffected():
    """Every wf2 variable carries an ask, so the filter must change nothing."""
    wf2 = [{"name": n, "ask": f"{n}?"} for n in
           ("property_type", "monthly_bill", "location", "roof_available",
            "customer_name", "assessment_agreed")]
    assert len(_questions(wf2)) == 6
