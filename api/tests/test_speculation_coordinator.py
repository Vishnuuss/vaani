"""The overlap layer — the thing that makes this a Vapi-shaped pipeline.

Dograh's pipeline is strictly reactive: nothing happens until the caller stops
talking, then STT -> LLM -> TTS run in sequence. Vapi-class systems overlap that
boundary — they start generating while the caller is still speaking and discard
the work if they guessed wrong.

    caller still speaking  ->  newest partial  ->  generation starts
    caller stops           ->  text matches?   ->  replay buffered tokens
                                                   (LLM time = 0)
                           ->  text differs?   ->  cancel, fall through

**The safety property, and it is structural.** This class never speaks. It hands
tokens back only when `take_response_for` is called, which happens only once the
turn detector has ended the turn, and only when the final text matches what was
speculated on EXACTLY. A wrong guess costs tokens and cannot reach the caller.
That is what makes speculation safe to run during someone's sentence: it changes
when the agent THINKS, never when it SPEAKS.

Updated 2026-08-29 for the trigger change. The old rule fired on the common
prefix of the last two partials, so these tests were written with the final
equal to the FIRST partial. It now fires on the newest partial, because the old
rule structurally excluded the last word and could never hit a short Telugu
answer -- see test_stable_prefix.py.

The LLM call is injected, so this is provider-agnostic and testable offline.
"""

import asyncio

import pytest

from api.services.pipecat.speculation.coordinator import SpeculationCoordinator


class FakeLLM:
    """Records what it was asked to generate and how often it was cancelled."""

    def __init__(self, reply="మీ ఇల్లు మీదే అయితే మంచిది అండి.", delay=0.0):
        self.reply = reply
        self.delay = delay
        self.calls: list[str] = []
        self.cancelled = 0

    async def __call__(self, text: str):
        self.calls.append(text)
        try:
            for token in self.reply.split():
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield token + " "
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def _settle():
    """Let the background generation task run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_it_starts_generating_while_the_caller_is_still_speaking():
    llm = FakeLLM()
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నా ఇల్లు")
    await coord.on_partial("నా ఇల్లు నాదే")
    await _settle()

    # The turn has NOT ended — take_response_for was never called — so this
    # proves generation began while the caller was still speaking.
    #
    # Only the newest survives: when partials arrive faster than a generation
    # starts, the superseded one is cancelled before its coroutine body ever
    # runs, so it costs nothing at all.
    assert llm.calls == ["నా ఇల్లు నాదే"]


@pytest.mark.asyncio
async def test_nothing_is_produced_until_the_turn_actually_ends():
    """The safety property. Speculation must change when it THINKS, not when
    it SPEAKS -- otherwise it would be an interruption."""
    llm = FakeLLM(reply="అలాగే అండి")
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నా ఇల్లు")
    await coord.on_partial("నా ఇల్లు నాదే")
    await _settle()

    # Generation ran, but the coordinator has handed nothing to anybody. The
    # only way tokens leave this object is take_response_for, and the turn
    # detector is what calls it.
    assert llm.calls, "it should have generated in the background"
    assert coord._task is not None, "the reply is buffered, not emitted"


@pytest.mark.asyncio
async def test_a_matching_turn_gets_the_pre_generated_response():
    llm = FakeLLM(reply="అలాగే అండి")
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నా ఇల్లు")
    await coord.on_partial("నా ఇల్లు నాదే")
    await _settle()

    tokens = await coord.take_response_for("నా ఇల్లు నాదే")

    assert tokens is not None
    assert "".join(tokens).strip() == "అలాగే అండి"
    before = len(llm.calls)
    assert await coord.take_response_for("నా ఇల్లు నాదే") is None
    assert len(llm.calls) == before, "must not call the LLM again on a hit"


@pytest.mark.asyncio
async def test_a_short_telugu_answer_is_a_hit():
    """The case the old trigger could never win: three words, spoken quickly.

    Run 274's caller said exactly this and was asked the same question three
    times; the speculation for it always fired one word short.
    """
    llm = FakeLLM(reply="సరే")
    coord = SpeculationCoordinator(generate=llm)

    for partial in ("వన్", "వన్ లాక్", "వన్ లాక్ అండి"):
        await coord.on_partial(partial)
    await _settle()

    assert await coord.take_response_for("వన్ లాక్ అండి") is not None


@pytest.mark.asyncio
async def test_a_mismatching_turn_gets_nothing_so_the_real_llm_runs():
    llm = FakeLLM()
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నాకు లోన్")
    await coord.on_partial("నాకు లోన్ కావాలి")
    await _settle()

    assert await coord.take_response_for("నాకు ఇల్లు కావాలి") is None


@pytest.mark.asyncio
async def test_a_caller_who_keeps_talking_is_never_answered_half_way():
    """Run 287: "మాది." then, a moment later, "ఇండస్ట్రీ ఉంది ఒకటి".

    Answering the fragment would be the interruption this project has spent the
    day removing. A prefix match is deliberately a MISS.
    """
    coord = SpeculationCoordinator(generate=FakeLLM())

    await coord.on_partial("మాది")
    await _settle()

    assert await coord.take_response_for("మాది ఇండస్ట్రీ ఉంది ఒకటి") is None


@pytest.mark.asyncio
async def test_a_turn_with_no_speculation_gets_nothing():
    coord = SpeculationCoordinator(generate=FakeLLM())

    assert await coord.take_response_for("సరే") is None


@pytest.mark.asyncio
async def test_a_backwards_revision_cancels_the_in_flight_generation():
    """Sarvam revises partials backwards as it re-scores (FINDINGS 5b)."""
    llm = FakeLLM(delay=0.01)  # slow enough to still be running
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నాకు లోన్")
    await coord.on_partial("నాకు లోన్ కావాలి")
    await _settle()
    assert llm.calls[-1] == "నాకు లోన్ కావాలి", "generation should be in flight"

    await coord.on_partial("నాకు ఇల్లు కావాలి")  # decoder revised — contradicts it
    await _settle()

    # The work must actually be torn down, not just ignored — otherwise a stale
    # generation for words the caller never said stays alive.
    assert llm.cancelled >= 1
    assert await coord.take_response_for("నాకు ఇల్లు కావాలి") is None


@pytest.mark.asyncio
async def test_the_response_can_only_be_taken_once():
    coord = SpeculationCoordinator(generate=FakeLLM(reply="సరే"))

    await coord.on_partial("అవును")
    await coord.on_partial("అవును సరే")
    await _settle()

    assert await coord.take_response_for("అవును సరే") is not None
    assert await coord.take_response_for("అవును సరే") is None


@pytest.mark.asyncio
async def test_it_records_hits_and_misses_for_the_latency_case():
    coord = SpeculationCoordinator(generate=FakeLLM(reply="సరే"))

    await coord.on_partial("అవును")
    await coord.on_partial("అవును సరే")
    await _settle()
    await coord.take_response_for("అవును సరే")  # hit

    coord.reset_turn()
    await coord.on_partial("కాదు")
    await coord.on_partial("కాదు లేదు")
    await _settle()
    await coord.take_response_for("వేరే మాట")  # miss

    assert coord.stats.hits == 1
    assert coord.stats.misses == 1
