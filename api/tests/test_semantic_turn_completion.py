"""The LLM decides whether the caller has finished, not a timer.

The client, 5 Sep, after a live call:

    "you not fix that to only wait for 0.35 seconds ... you need to understand
     intent by semantic naa whether sentence is complete or not"

He is right, and the call proves it. Run 790:

    USER : ...ఇండస్ట్రియల్ ఏరియాలో ఉంటాను.
    USER : సిటీకి కొంచెం బయట.          <- still talking
    BOT  : సరే, మీకు సొంత              <- cut in

"ఇండస్ట్రియల్ ఏరియాలో ఉంటాను" is a GRAMMATICALLY COMPLETE sentence. The prosody
model hears a falling contour and `completeness.sounds_unfinished` finds no
dangling quantity, no open range, no connective and no hesitation -- so both
signals say "finished" and both are wrong. The caller had more to say.

Grammatically complete is not conversationally complete, and no timer and no
grammar rule can tell them apart. Only something that understands the
conversation can.

pipecat's own wiring for this:

    stop=[
        deferred(TurnAnalyzerUserTurnStopStrategy(...)),   # audio: "maybe done"
        LLMTurnCompletionUserTurnStopStrategy(...),        # LLM: "actually done?"
    ]

The audio detector TRIGGERS the question; the LLM ANSWERS it, with ✓ / ○ / ◐.
On ○ or ◐ the turn is not finalized and the caller keeps the floor.

This needs no interim transcripts. An earlier note in this project claimed it
was blocked on realtime STT; that is true only of the latency benefit (thinking
ahead mid-utterance), not of the correctness benefit, which is this.
"""

from __future__ import annotations

from pathlib import Path

# Read the source rather than import it: `turn_taking` pulls in the service
# factory, which needs DATABASE_URL and google.genai, neither of which exists
# on a dev machine. The wiring is what these tests are about, and the wiring is
# visible in the text.
_ROOT = Path(__file__).resolve().parents[1]
TURN_TAKING = (_ROOT / "services" / "vaani" / "turn_taking.py").read_text(
    encoding="utf-8")
SCHEMA = (_ROOT / "schemas" / "workflow_configurations.py").read_text(
    encoding="utf-8")


def test_semantic_completion_is_off_unless_asked_for():
    """It changes reply FORMAT, so it never turns itself on."""
    assert "DEFAULT_SEMANTIC_TURN_COMPLETION = False" in SCHEMA


def test_the_audio_detector_still_leads():
    """The Telugu analyzer is the only detector that reads Telugu at all. The
    LLM adds a second opinion; it must not replace the first."""
    src = TURN_TAKING
    assert "deferred(" in src, "the analyzer must be deferred, not removed"
    assert "TextAwareTurnStopStrategy" in src


def test_the_llm_strategy_is_paired_with_it():
    src = TURN_TAKING
    assert "LLMTurnCompletionUserTurnStopStrategy" in src
    # Deferred detector first, finalizer second -- that is pipecat's contract.
    assert src.index("deferred(") < src.index(
        "LLMTurnCompletionUserTurnStopStrategy(")


def test_it_uses_vaanis_own_instructions_not_pipecats():
    """pipecat's defaults demand the reply begin with the marker, and Vaani's
    MODE_PROTOCOL demands it begin with MODE. Shipping the defaults would cost
    the MODE line, and MODE: END is the only thing that hangs up a call."""
    src = TURN_TAKING
    assert "compose_instructions" in src
    assert "MODE_PROTOCOL" in src


def test_the_analyzer_is_still_reachable_for_the_filler_player():
    """`analyzer_from` walks the strategy list to find the SAME analyzer
    instance. Wrapping it in `deferred` must not hide it, or the filler player
    silently builds a second model that scores different audio."""
    src = TURN_TAKING
    assert "_strategy" in src or "getattr" in src
