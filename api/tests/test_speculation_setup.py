"""Regression test for the bug that silently disabled speculation on every call.

The setup was inlined in run_pipeline and passed `workflow` (a WorkflowModel —
the database row) where a WorkflowGraph was required. `WorkflowModel` has no
`.nodes`, so it raised AttributeError, the surrounding try/except logged one
WARNING line, and every call ran with speculation off. Nothing failed loudly;
the feature simply never existed at runtime.

This test builds a real WorkflowGraph the way the live path does and asserts the
setup produces a working probe/gate pair sharing one coordinator.
"""

from types import SimpleNamespace

import pytest

from api.services.pipecat.run_pipeline import build_speculation_processors
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.workflow_graph import WorkflowGraph


def _single_prompt_graph():
    """A one-node, zero-edge workflow — the shape the /agent editor writes."""
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
                        },
                    }
                ],
                "edges": [],
            }
        )
    )


class _FakeSettings:
    model = "openai/gpt-oss-120b"


class _FakeLLM:
    _client = object()
    _settings = _FakeSettings()


def test_setup_succeeds_with_a_real_workflow_graph():
    probe, gate = build_speculation_processors(
        _single_prompt_graph(),
        _FakeLLM(),
        SimpleNamespace(messages=[]),
        has_recordings=False,
    )

    assert probe is not None and gate is not None


def test_probe_and_gate_share_one_coordinator():
    # They sit at different points in the pipeline and must agree on state:
    # the probe sees partials, the gate makes the replay decision.
    probe, gate = build_speculation_processors(
        _single_prompt_graph(),
        _FakeLLM(),
        SimpleNamespace(messages=[]),
        has_recordings=False,
    )

    assert probe._coordinator is gate._coordinator


def test_passing_the_db_row_instead_of_the_graph_fails_loudly():
    # This is the exact mistake that shipped. It must raise, not return a
    # half-built pair that silently never speculates.
    workflow_model_like = SimpleNamespace(id=5, name="MB Solar Hub")

    with pytest.raises(AttributeError):
        build_speculation_processors(
            workflow_model_like,
            _FakeLLM(),
            SimpleNamespace(messages=[]),
            has_recordings=False,
        )
