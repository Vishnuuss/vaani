"""What you type in the UI is Layer 3. Vaani supplies the other three.

The gap this closes: the brain was wired in, but the SYSTEM PROMPT was still
Dograh's raw node text. Vaani's persona / psychology / mission layers sat in
`vaani/layers/` and were never loaded, so the agent had Vaani's plumbing and
none of its selling.

    Layer 1  persona & voice     from layers/01_persona/<language>.md
    Layer 2  sales psychology    from layers/02_psychology/core.md   <- the moat
    Layer 3  business            WHAT THE USER TYPES IN THE UI
    Layer 4  mission             from layers/04_mission/<agent_type>.md

Layers 1, 2 and 4 are byte-identical across every agent, so they sit at the
FRONT and land in the provider's cached prefix.
"""

from types import SimpleNamespace

from api.services.pipecat.run_pipeline import compile_vaani_system_prompt
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.workflow_graph import WorkflowGraph

UI_TEXT = "MB Solar Hub connects customers to verified solar vendors."


def _graph(prompt=UI_TEXT, variables=None):
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
                            "prompt": prompt,
                            "greeting": "Namaskaram andi.",
                            "greeting_type": "text",
                            "is_start": True,
                            "add_global_prompt": False,
                            "extraction_enabled": bool(variables),
                            "extraction_variables": variables or [],
                        },
                    }
                ],
                "edges": [],
            }
        )
    )


def test_the_ui_text_survives_into_the_compiled_prompt():
    prompt = compile_vaani_system_prompt(_graph(), workflow_name="MB Solar Hub")

    assert UI_TEXT in prompt


def test_vaani_adds_the_psychology_layer_the_user_never_types():
    prompt = compile_vaani_system_prompt(_graph(), workflow_name="MB Solar Hub")

    # Layer 2 is the moat — objection handling that ships with every agent.
    assert "Layer 2" in prompt
    assert len(prompt) > len(UI_TEXT) * 10, "layers were not loaded"


def test_the_persona_layer_is_present():
    prompt = compile_vaani_system_prompt(_graph(), workflow_name="MB Solar Hub")

    assert "Layer 1" in prompt


def test_extraction_variables_become_the_questions_in_layer_3():
    prompt = compile_vaani_system_prompt(
        _graph(variables=[{"name": "monthly_bill", "type": "number",
                           "prompt": "Monthly electricity bill"}]),
        workflow_name="MB Solar Hub",
    )

    assert "Monthly electricity bill" in prompt


def test_the_cached_prefix_is_identical_across_two_different_agents():
    # Layers 1+2 are constant, so two agents share a long identical prefix.
    a = compile_vaani_system_prompt(_graph("Business A"), workflow_name="A")
    b = compile_vaani_system_prompt(_graph("Business B"), workflow_name="B")

    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    assert shared > 2000, f"only {shared} chars of shared prefix — cache will miss"
