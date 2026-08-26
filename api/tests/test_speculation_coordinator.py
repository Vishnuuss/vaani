"""The overlap layer — the thing that makes this a Vapi-shaped pipeline.

Dograh's pipeline is strictly reactive: nothing happens until the caller stops
talking, then STT -> LLM -> TTS run in sequence. Vapi-class systems overlap that
boundary — they start generating while the caller is still speaking and discard
the work if they guessed wrong.

This coordinator owns that overlap:

    caller still speaking  ->  stable prefix appears  ->  generation starts
    caller stops           ->  text matches?          ->  replay buffered tokens
                                                          (LLM time = 0)
                           ->  text differs?          ->  cancel, fall through

The LLM call itself is injected, so this is provider-agnostic and testable
without a network.
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
    await coord.on_partial("నా ఇల్లు నాదే")  # prefix "నా ఇల్లు" is now stable
    await _settle()  # let the scheduled task actually enter the generator

    # The turn has NOT ended — take_response_for was never called — so this
    # proves generation began while the caller was still speaking.
    assert llm.calls == ["నా ఇల్లు"], "should have started generating mid-turn"


@pytest.mark.asyncio
async def test_a_matching_turn_gets_the_pre_generated_response():
    llm = FakeLLM(reply="అలాగే అండి")
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నా ఇల్లు")
    await coord.on_partial("నా ఇల్లు నాదే")
    await _settle()

    tokens = await coord.take_response_for("నా ఇల్లు")

    assert tokens is not None
    assert "".join(tokens).strip() == "అలాగే అండి"
    assert len(llm.calls) == 1, "must not call the LLM a second time on a hit"


@pytest.mark.asyncio
async def test_a_mismatching_turn_gets_nothing_so_the_real_llm_runs():
    llm = FakeLLM()
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నాకు లోన్")
    await coord.on_partial("నాకు లోన్ కావాలి")
    await _settle()

    tokens = await coord.take_response_for("నాకు ఇల్లు కావాలి")

    assert tokens is None


@pytest.mark.asyncio
async def test_a_turn_with_no_speculation_gets_nothing():
    coord = SpeculationCoordinator(generate=FakeLLM())

    assert await coord.take_response_for("సరే") is None


@pytest.mark.asyncio
async def test_a_backwards_revision_cancels_the_in_flight_generation():
    llm = FakeLLM(delay=0.01)  # slow enough to still be running
    coord = SpeculationCoordinator(generate=llm)

    await coord.on_partial("నాకు లోన్")
    await coord.on_partial("నాకు లోన్ కావాలి")  # fires on "నాకు లోన్"
    await _settle()
    assert llm.calls == ["నాకు లోన్"], "generation should be in flight"

    await coord.on_partial("నాకు ఇల్లు కావాలి")  # decoder revised — contradicts it
    await _settle()

    # The work must actually be torn down, not just ignored — otherwise a
    # stale generation for words the caller never said stays alive.
    assert llm.cancelled == 1
    assert await coord.take_response_for("నాకు ఇల్లు కావాలి") is None


@pytest.mark.asyncio
async def test_the_response_can_only_be_taken_once():
    coord = SpeculationCoordinator(generate=FakeLLM(reply="సరే"))

    await coord.on_partial("అవును")
    await coord.on_partial("అవును సరే")
    await _settle()

    assert await coord.take_response_for("అవును") is not None
    assert await coord.take_response_for("అవును") is None


@pytest.mark.asyncio
async def test_it_records_hits_and_misses_for_the_latency_case():
    coord = SpeculationCoordinator(generate=FakeLLM(reply="సరే"))

    await coord.on_partial("అవును")
    await coord.on_partial("అవును సరే")
    await _settle()
    await coord.take_response_for("అవును")  # hit

    coord.reset_turn()
    await coord.on_partial("కాదు")
    await coord.on_partial("కాదు లేదు")
    await _settle()
    await coord.take_response_for("వేరే మాట")  # miss

    assert coord.stats.hits == 1
    assert coord.stats.misses == 1
