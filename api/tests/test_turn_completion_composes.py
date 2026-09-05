"""The completion marker and the MODE line must not fight over the first token.

pipecat's default instructions say "Every single response MUST begin with a turn
completion indicator". Vaani's MODE_PROTOCOL says "the first line of your reply
is always exactly one of: MODE: ASK ...". Shipping the defaults unchanged would
cost the MODE line, and MODE is what ends calls -- `MODE: END` is the only thing
that sets `state.must_end`. Losing it does not make the agent worse at talking;
it makes it unable to hang up.
"""

from __future__ import annotations

from api.services.vaani.compiler import MODE_PROTOCOL
from api.services.vaani.turn_completion import (
    VAANI_TURN_COMPLETION_INSTRUCTIONS,
    compose_instructions,
)


def test_all_three_markers_are_defined():
    for marker in ("✓", "○", "◐"):
        assert marker in VAANI_TURN_COMPLETION_INSTRUCTIONS


def test_the_marker_is_ordered_before_the_mode_line():
    text = VAANI_TURN_COMPLETION_INSTRUCTIONS
    assert text.index("✓ MODE: ASK") > 0, "the worked example must show the order"
    assert "a turn completion marker" in text
    assert text.index("a turn completion marker") < text.index("the MODE line")


def test_the_incomplete_markers_forbid_any_other_output():
    """A stray word after ○ would be spoken to a caller who is mid-sentence."""
    text = VAANI_TURN_COMPLETION_INSTRUCTIONS
    assert "NOTHING else" in text
    assert "No MODE line" in text


def test_composing_keeps_one_source_of_truth_for_MODE():
    composed = compose_instructions(MODE_PROTOCOL)
    assert MODE_PROTOCOL in composed, "MODE text must not be copied, only appended"
    assert composed.index("✓") < composed.index("MODE: END")


def test_all_three_modes_survive_composition():
    composed = compose_instructions(MODE_PROTOCOL)
    for mode in ("MODE: ASK", "MODE: CLOSE", "MODE: END"):
        assert mode in composed


def test_the_bias_is_toward_answering():
    """A false 'incomplete' leaves a caller sitting in silence, which is the
    failure they actually notice and complain about."""
    assert "When in doubt use ✓" in VAANI_TURN_COMPLETION_INSTRUCTIONS
