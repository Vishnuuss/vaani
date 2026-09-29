"""The live phone path must remember what the caller already answered.

This is the regression test for the defect found on 2026-09-10: `state.known`
was filled only by `vaani/brain/extractor.py`, and the extractor was imported
by the SIMULATOR only. `StateInjector` — the processor that actually runs on a
phone call — never called it.

The consequence on every real call:

    KNOWN stays {} forever
      -> still_need lists all fields on every turn
      -> render() re-pins `NEXT QUESTION TO ASK: "<question 1>"` as the last,
         loudest instruction in the context, however many times it was answered
      -> advance() can never leave QUALIFYING, because that needs still_need
         to be empty

The agent therefore asks question one again and again. The extractor's own
docstring predicted exactly this, and the sim gate could never catch it because
the sim wires up the extractor itself.

These tests drive the LIVE processor, not the simulator. If someone ever
unwires it again, this file fails.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import StartFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

from vaani.brain.compiler import Brief
from vaani.gateway.brain_processor import StateInjector

BRIEF = Brief(
    business="Test Solar",
    language="te-IN",
    questions=[
        {"field": "home_ownership", "ask": "Do you own or rent your home?"},
        {"field": "monthly_bill", "ask": "What is your monthly electricity bill?"},
        {"field": "timeline", "ask": "When are you looking to install?"},
    ],
)


class FakeContext:
    """Stands in for the pipecat LLM context. Records what was set."""

    def __init__(self, messages=None):
        self.messages = list(messages or [])

    def set_messages(self, messages):
        self.messages = list(messages)


class FakeLLM:
    """A stub extraction model. Returns canned JSON and records its calls."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **kwargs) -> str:
        self.calls.append(messages)
        return json.dumps(self._payload)


async def _drive(injector: StateInjector, text: str) -> None:
    """Push one final transcript through the processor, as the pipeline would.

    The processor is started the way a real pipeline starts it — its own task
    manager and clock, then a StartFrame — so this exercises the live code
    path rather than a hand-rolled imitation of it.
    """
    manager = TaskManager()
    manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    await injector.setup(
        FrameProcessorSetup(
            clock=SystemClock(), task_manager=manager, pipeline_worker=None
        )
    )
    await injector.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    await injector.process_frame(
        TranscriptionFrame(text=text, user_id="caller", timestamp=""),
        FrameDirection.DOWNSTREAM,
    )
    # Extraction is fire-and-forget by design so it never delays a reply. The
    # pipeline gets its result on the next turn; the test waits for it here.
    if injector.extraction is not None:
        await injector.extraction


def test_caller_answer_is_learned_on_the_live_path():
    """An answered question must land in KNOWN, not just in the transcript."""
    llm = FakeLLM({"home_ownership": "own"})
    injector = StateInjector(BRIEF, FakeContext(), "SYSTEM", llm=llm)

    asyncio.run(_drive(injector, "మా ఇల్లు మాదే"))

    assert injector.state.known.get("home_ownership") == "own", (
        "the caller answered question one and the live path did not record it; "
        f"KNOWN={injector.state.known}. STILL_NEED will keep listing it and the "
        "agent will ask it again."
    )


def test_answered_question_leaves_still_need():
    """The whole point of KNOWN: the checklist must shrink."""
    llm = FakeLLM({"home_ownership": "own"})
    injector = StateInjector(BRIEF, FakeContext(), "SYSTEM", llm=llm)

    asyncio.run(_drive(injector, "మా ఇల్లు మాదే"))

    assert "home_ownership" not in injector.state.still_need, (
        f"still_need={injector.state.still_need} — an answered field is still "
        "on the checklist"
    )


def test_answered_question_is_not_re_pinned_as_the_next_question():
    """`NEXT QUESTION TO ASK` is the loudest line in the context.

    Re-pinning an answered question there is the mechanism of the loop, so it
    is asserted directly on the rendered block.
    """
    llm = FakeLLM({"home_ownership": "own"})
    injector = StateInjector(BRIEF, FakeContext(), "SYSTEM", llm=llm)

    asyncio.run(_drive(injector, "మా ఇల్లు మాదే"))

    block = injector.state.render()
    assert "Do you own or rent your home?" not in block, (
        "the answered question is still being pinned as the next one to ask:\n"
        + block
    )


def test_extraction_sees_what_the_agent_had_just_said():
    """"Own" only means home ownership in the context of the question asked.

    Without the agent's previous line the extractor is guessing, so the live
    path must pass it — the simulator already does.
    """
    llm = FakeLLM({"home_ownership": "own"})
    context = FakeContext([
        {"role": "assistant", "content": "మీ ఇల్లు సొంతమా అద్దెనా?"},
    ])
    injector = StateInjector(BRIEF, context, "SYSTEM", llm=llm)

    asyncio.run(_drive(injector, "మా ఇల్లు మాదే"))

    assert llm.calls, "the extractor was never called on the live path"
    sent = str(llm.calls[-1])
    assert "మీ ఇల్లు సొంతమా అద్దెనా?" in sent, (
        "the agent's own previous line was not given to the extractor"
    )


def test_a_dead_extraction_model_never_breaks_the_call():
    """Extraction is a nice-to-have. A failing model must not raise into the
    audio path — a silent agent is far worse than a repeated question."""

    class BrokenLLM:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("provider down")

    injector = StateInjector(BRIEF, FakeContext(), "SYSTEM", llm=BrokenLLM())

    asyncio.run(_drive(injector, "మా ఇల్లు మాదే"))

    assert injector.state.known == {}, "nothing should have been learned"


def test_no_extraction_model_configured_is_survivable():
    """The injector must still run when no extraction LLM is supplied, so an
    unconfigured deployment degrades to the old behaviour instead of crashing."""
    injector = StateInjector(BRIEF, FakeContext(), "SYSTEM")

    asyncio.run(_drive(injector, "మా ఇల్లు మాదే"))

    assert injector.state.turn == 1


def test_hard_stops_still_run_synchronously():
    """Triage must keep working on the same path — it is what ends calls."""
    llm = FakeLLM({})
    injector = StateInjector(BRIEF, FakeContext(), "SYSTEM", llm=llm)

    asyncio.run(_drive(injector, "నా నంబర్ లిస్ట్ నుంచి తీసేయండి"))

    assert injector.state.must_end, "a removal request no longer ends the call"


def test_the_live_pipeline_hands_the_injector_an_extraction_model():
    """The defect was never in the brain — it was in the wiring.

    `extractor` was wired into the simulator and not into the pipeline, so
    every sim gate passed while every real call looped. A unit test of the
    processor cannot catch that: the processor was fine. So this asserts on
    the construction site itself, which is the thing that was actually wrong.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1]
              / "vaani" / "gateway" / "pipeline.py").read_text(encoding="utf-8")

    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)
             and getattr(node.func, "id", "") == "StateInjector"]

    assert calls, "the pipeline no longer constructs a StateInjector at all"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "llm" in kwargs, (
            f"pipeline.py:{call.lineno} builds a StateInjector with no `llm=`. "
            "KNOWN will stay empty on every live call and the agent will re-ask "
            "question one for the whole call."
        )
