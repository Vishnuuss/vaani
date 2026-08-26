"""Vaani's brain, built from a Dograh workflow.

Dograh keeps campaigns, Redis, Postgres, telephony and the dashboard. The
conversation itself is Vaani's: triage plus a live state block before the LLM,
guardrails after it.

The bridge is this: a Dograh single-prompt agent already carries everything a
Vaani `Brief` needs — the prompt, and the extraction variables, which ARE the
qualification questions. `field` comes from the variable name and `ask` from its
description, so the state block names real fields instead of derived guesses.
"""

from types import SimpleNamespace

import pytest

from api.services.pipecat.run_pipeline import build_vaani_brain
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.workflow_graph import WorkflowGraph


def _graph(extraction_variables=None):
    return WorkflowGraph(
        ReactFlowDTO.model_validate(
            {
                "nodes": [
                    {
                        "id": "agent",
                        "type": "startCall",
                        "position": {"x": 0, "y": 0},
                        "data": {
                            "name": "Agent",
                            "prompt": "You are Priya from MB Solar Hub.",
                            "greeting": "Namaskaram andi.",
                            "greeting_type": "text",
                            "is_start": True,
                            "allow_interrupt": True,
                            "add_global_prompt": False,
                            "extraction_enabled": bool(extraction_variables),
                            "extraction_variables": extraction_variables or [],
                        },
                    }
                ],
                "edges": [],
            }
        )
    )


def _context():
    return SimpleNamespace(messages=[])


def test_the_extraction_variables_become_the_qualification_questions():
    injector, _ = build_vaani_brain(
        _graph(
            [
                {"name": "monthly_bill", "type": "number", "prompt": "Monthly bill"},
                {"name": "city", "type": "string", "prompt": "Which city"},
            ]
        ),
        _context(),
        "SYSTEM",
        workflow_name="MB Solar Hub",
    )

    assert injector.state.required_fields == ["monthly_bill", "city"]


def test_the_question_text_comes_from_the_variable_description():
    injector, _ = build_vaani_brain(
        _graph([{"name": "city", "type": "string", "prompt": "Which city"}]),
        _context(),
        "SYSTEM",
        workflow_name="MB Solar Hub",
    )

    assert injector.state.questions["city"] == "Which city"


def test_the_reply_filter_guards_the_same_injector():
    injector, reply_filter = build_vaani_brain(
        _graph([{"name": "city", "type": "string", "prompt": "Which city"}]),
        _context(),
        "SYSTEM",
        workflow_name="MB Solar Hub",
    )

    assert reply_filter._injector is injector


def test_an_agent_with_no_extraction_variables_still_builds():
    # Not every agent qualifies. The brain must still run for triage and
    # guardrails rather than refusing to start.
    injector, reply_filter = build_vaani_brain(
        _graph([]), _context(), "SYSTEM", workflow_name="Support bot"
    )

    assert injector is not None and reply_filter is not None
    assert injector.state.required_fields == []


def test_passing_the_db_row_instead_of_the_graph_fails_loudly():
    with pytest.raises(AttributeError):
        build_vaani_brain(
            SimpleNamespace(id=5, name="MB Solar Hub"),
            _context(),
            "SYSTEM",
            workflow_name="MB Solar Hub",
        )
