"""Not every human sound is an attempt to take the floor.

The defect
----------
`llm_response_universal.py` broadcasts an interruption on EVERY user-turn
start::

    if params.enable_interruptions:
        await self.broadcast_interruption()

`enable_interruptions` defaults to True, and the live MB Solar config is
`turn_start_strategy: min_words` with `turn_start_min_words: 1`. So the first
interim transcript carrying one word stops the bot mid-sentence -- and Telugu
callers emit "ఆ" / "సరే" / "అవును" continuously while they are LISTENING.
There is no minimum duration, no minimum energy and no confidence check
anywhere between the noise and the interruption.

The opposite failure is worse, so these tests pin both sides: "ఆగండి" (wait)
and a real 900 ms utterance must still stop the bot instantly, and with the
gate at its default the behaviour must be exactly what it is today.
"""

from __future__ import annotations

import inspect
import struct
from pathlib import Path

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
    UserTurnStartedParams,
)
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)

from api.services.vaani.barge_in import (
    BargeInGate,
    BargeInGatedUserTurnStartStrategy,
    BargeInParams,
    apply_barge_in_gate,
    has_interrupt_word,
    is_backchannel,
    normalise,
    resolve_barge_in_params,
    rms_int16,
)

# The gate as an operator would actually switch it on: master switch plus the
# shipped defaults.
ON = BargeInParams(
    enabled=True,
    min_speech_secs=0.35,
    min_rms_ratio=1.8,
    max_backchannel_words=2,
    lexical=True,
)


def _speaking_gate(params: BargeInParams = ON, *, onset: float = 0.0) -> BargeInGate:
    """A gate mid-call: the bot is talking and the caller has just started."""
    gate = BargeInGate(params)
    gate.note_bot_speaking(True)
    gate.note_speech_started(onset)
    return gate


# --- duration ---------------------------------------------------------------


def test_a_150ms_blip_does_not_interrupt():
    """A cough, a line click, or one filler syllable. Not a turn grab."""
    gate = _speaking_gate(onset=0.0)
    decision = gate.should_interrupt(now=0.150)
    assert decision.interrupt is False
    assert decision.reason.startswith("too-short")


def test_a_900ms_utterance_does_interrupt():
    """Nine hundred milliseconds is a sentence. The caller has the floor."""
    gate = _speaking_gate(onset=0.0)
    assert gate.should_interrupt(now=0.900).interrupt is True


def test_the_boundary_is_the_configured_number_and_nothing_else():
    """No hidden millisecond anywhere: move the config, the boundary moves."""
    slow = BargeInParams(enabled=True, min_speech_secs=0.9, min_rms_ratio=0.0)
    assert _speaking_gate(slow, onset=0.0).should_interrupt(now=0.5).interrupt is False

    quick = BargeInParams(enabled=True, min_speech_secs=0.1, min_rms_ratio=0.0)
    assert _speaking_gate(quick, onset=0.0).should_interrupt(now=0.5).interrupt is True


def test_an_unseen_onset_does_not_invent_a_duration():
    """If VAD never told us when speech began, do not guess -- let it through.

    Guessing here would silently mute a caller on any transport whose VAD
    frames do not reach the aggregator.
    """
    gate = BargeInGate(ON)
    gate.note_bot_speaking(True)
    assert gate.should_interrupt(now=1_000.0).interrupt is True


# --- the bot has to actually be speaking ------------------------------------


def test_a_blip_while_the_bot_is_silent_is_left_completely_alone():
    """There is nothing to interrupt, so there is nothing to gate.

    This is the check that keeps the gate from touching ordinary turn-taking.
    A 150 ms answer to a closed question -- "ఆ" meaning yes -- must still start
    a turn and still be answered when the bot is not talking.
    """
    gate = BargeInGate(ON)
    gate.note_bot_speaking(False)
    gate.note_speech_started(0.0)
    gate.note_text("ఆ")
    decision = gate.should_interrupt(now=0.150)
    assert decision.interrupt is True
    assert decision.reason == "bot-not-speaking"


def test_the_bot_speaking_flag_follows_the_frames():
    gate = BargeInGate(ON)
    strategy = BargeInGatedUserTurnStartStrategy(
        MinWordsUserTurnStartStrategy(min_words=1), gate
    )
    strategy.observe(BotStartedSpeakingFrame())
    assert gate._bot_speaking is True
    strategy.observe(BotStoppedSpeakingFrame())
    assert gate._bot_speaking is False


# --- lexical ----------------------------------------------------------------


def test_aa_during_bot_speech_does_not_interrupt():
    """"ఆ" is the caller saying *go on*. It must not stop the bot."""
    gate = _speaking_gate(onset=0.0)
    gate.note_text("ఆ")
    decision = gate.should_interrupt(now=5.0)  # long enough to pass duration
    assert decision.interrupt is False
    assert decision.reason == "backchannel"


def test_aagandi_interrupts_even_below_the_duration_floor():
    """"ఆగండి" (wait) is the caller stopping the agent. It has to land NOW.

    Short enough that the duration check would have refused it -- the lexical
    check runs first for exactly this reason.
    """
    gate = _speaking_gate(onset=0.0)
    gate.note_text("ఆగండి")
    decision = gate.should_interrupt(now=0.200)
    assert decision.interrupt is True
    assert decision.reason == "interrupt-word"


def test_the_other_real_interruptions_land_too():
    for said in ["వద్దు", "ఒక నిమిషం", "ఒక్క నిమిషం", "కాదు", "ఆపండి"]:
        gate = _speaking_gate(onset=0.0)
        gate.note_text(said)
        assert gate.should_interrupt(now=0.200).interrupt is True, said


def test_the_other_real_backchannels_are_held():
    for said in ["సరే", "అవును", "హా", "ఓకే", "ఆ ఆ"]:
        gate = _speaking_gate(onset=0.0)
        gate.note_text(said)
        assert gate.should_interrupt(now=5.0).interrupt is False, said


def test_a_sentence_that_merely_opens_with_sare_is_not_a_backchannel():
    """"సరే, నాకు ఇప్పుడు టైం లేదు" is a caller leaving. Let it through."""
    gate = _speaking_gate(onset=0.0)
    gate.note_text("సరే నాకు ఇప్పుడు టైం లేదు")
    assert gate.should_interrupt(now=5.0).interrupt is True


def test_normalise_keeps_telugu_matras():
    """The obvious `[^\\w\\s]+` silently destroys every Telugu word.

    Python's `\\w` does not match Telugu combining marks, so that pattern turns
    "ఆగండి" into "ఆగ డ" -- which matches nothing in either lexicon. The gate
    would have looked like it was ignoring the transcript entirely, with no
    error anywhere.
    """
    assert normalise("ఆగండి!") == "ఆగండి"
    assert normalise("ఒక్క నిమిషం,") == "ఒక్క నిమిషం"
    assert has_interrupt_word("ఆగండి!") is True
    assert is_backchannel("సరే,") is True


def test_word_count_bounds_the_backchannel_rule():
    assert is_backchannel("సరే అండి", max_words=2) is True
    assert is_backchannel("సరే అండి సార్", max_words=2) is False
    assert is_backchannel("సరే అండి సార్", max_words=3) is True


def test_an_interrupt_word_beats_a_backchannel_in_the_same_utterance():
    """"సరే ఆగండి" -- fine, wait. The stop word wins."""
    assert is_backchannel("సరే ఆగండి") is False
    assert has_interrupt_word("సరే ఆగండి") is True


# --- energy -----------------------------------------------------------------


def _pcm(amplitude: int, samples: int = 160) -> bytes:
    """`samples` of a square wave, so RMS is exactly `amplitude`."""
    return struct.pack(f"<{samples}h", *([amplitude, -amplitude] * (samples // 2)))


def test_rms_matches_the_signal():
    assert abs(rms_int16(_pcm(1000)) - 1000) < 1
    assert rms_int16(b"") is None


def test_speech_quieter_than_the_line_floor_does_not_interrupt():
    """Crosstalk and TV in the background are not the caller taking the floor."""
    gate = BargeInGate(ON)
    gate.note_bot_speaking(True)
    for _ in range(200):          # learn the ambient floor first
        gate.note_audio(_pcm(300))
    gate.note_speech_started(0.0)
    for _ in range(10):
        gate.note_audio(_pcm(350))  # barely above the floor
    decision = gate.should_interrupt(now=5.0)
    assert decision.interrupt is False
    assert decision.reason.startswith("quiet")


def test_speech_well_above_the_floor_does_interrupt():
    gate = BargeInGate(ON)
    gate.note_bot_speaking(True)
    for _ in range(200):
        gate.note_audio(_pcm(300))
    gate.note_speech_started(0.0)
    for _ in range(10):
        gate.note_audio(_pcm(4000))
    assert gate.should_interrupt(now=5.0).interrupt is True


def test_the_energy_check_is_skipped_until_a_floor_exists():
    """It must never mute the opening seconds of a call, before any audio."""
    gate = _speaking_gate(onset=0.0)
    assert gate._floor is None
    assert gate.should_interrupt(now=5.0).interrupt is True


def test_the_ambient_floor_does_not_learn_from_the_caller():
    """A floor that rose with the caller's own voice would gate them out."""
    gate = BargeInGate(ON)
    for _ in range(50):
        gate.note_audio(_pcm(300))
    quiet_floor = gate._floor
    gate.note_speech_started(0.0)
    for _ in range(200):
        gate.note_audio(_pcm(9000))
    assert gate._floor == quiet_floor


# --- the default is today ---------------------------------------------------


def test_disabled_is_the_default_and_allows_everything():
    """Nothing changes until someone sets `barge_in_gate_enabled`."""
    gate = BargeInGate(resolve_barge_in_params({}))
    gate.note_bot_speaking(True)
    gate.note_speech_started(0.0)
    gate.note_text("ఆ")
    decision = gate.should_interrupt(now=0.001)
    assert decision.interrupt is True
    assert decision.reason == "gate-disabled"


def test_apply_returns_the_very_same_objects_when_disabled():
    """Not a copy and not a permissive wrapper -- the identical list.

    The strongest available statement of "byte-identical to today": with the
    default config there is no new object in the pipeline at all, so there is
    no code path that could behave differently.
    """
    inner = MinWordsUserTurnStartStrategy(min_words=1)
    strategies = [inner]
    assert apply_barge_in_gate(strategies, {}) is strategies
    assert apply_barge_in_gate(strategies, {})[0] is inner
    # Explicitly false is the same as absent.
    assert apply_barge_in_gate(strategies, {"barge_in_gate_enabled": False}) is strategies


def test_apply_wraps_only_when_switched_on():
    inner = MinWordsUserTurnStartStrategy(min_words=1)
    wrapped = apply_barge_in_gate([inner], {"barge_in_gate_enabled": True})
    assert isinstance(wrapped[0], BargeInGatedUserTurnStartStrategy)
    assert wrapped[0].inner is inner


def test_every_number_comes_from_the_config():
    """No hardcoded milliseconds. Standing project rule."""
    params = resolve_barge_in_params({
        "barge_in_gate_enabled": True,
        "barge_in_min_speech_secs": 0.75,
        "barge_in_min_rms_ratio": 3.5,
        "barge_in_max_backchannel_words": 4,
        "barge_in_lexical": False,
    })
    assert params.enabled is True
    assert params.min_speech_secs == 0.75
    assert params.min_rms_ratio == 3.5
    assert params.max_backchannel_words == 4
    assert params.lexical is False


def test_the_config_keys_exist_on_the_schema():
    """A key the dashboard cannot store is a key that does nothing.

    Migration 004 and the retry cap both shipped as code whose config was never
    reachable; this is the cheap guard against repeating it.
    """
    from api.schemas.workflow_configurations import WorkflowConfigurationDefaults

    defaults = WorkflowConfigurationDefaults()
    assert defaults.barge_in_gate_enabled is False
    assert defaults.barge_in_lexical is True
    assert defaults.barge_in_min_speech_secs > 0
    configured = WorkflowConfigurationDefaults(
        barge_in_gate_enabled=True, barge_in_min_speech_secs=0.5
    )
    assert configured.barge_in_gate_enabled is True
    assert configured.barge_in_min_speech_secs == 0.5


# --- the wrapper, end to end ------------------------------------------------


class _Recorder:
    """Records the params each `on_user_turn_started` carried.

    `handler` is a real async FUNCTION, not this object: pipecat dispatches on
    `inspect.iscoroutinefunction`, which is False for a class with an async
    `__call__`, so a callable instance is invoked synchronously and its
    coroutine is silently never awaited. The recorder then stays empty and the
    test fails for a reason that has nothing to do with the gate.
    """

    def __init__(self):
        self.params: list[UserTurnStartedParams] = []

    def handler(self):
        async def _record(_strategy, params):
            self.params.append(params)

        return _record


def _attach(strategy) -> _Recorder:
    recorder = _Recorder()
    # This is the handler UserTurnController._setup_strategies registers.
    strategy.add_event_handler("on_user_turn_started", recorder.handler())
    return recorder


def _wire(gate: BargeInGate, *, min_words: int = 1):
    inner = MinWordsUserTurnStartStrategy(min_words=min_words)
    wrapper = BargeInGatedUserTurnStartStrategy(inner, gate)
    return wrapper, _attach(wrapper)


def _interim(text: str) -> InterimTranscriptionFrame:
    return InterimTranscriptionFrame(user_id="caller", text=text, timestamp="0")


async def test_a_backchannel_still_starts_the_turn_but_does_not_stop_the_bot():
    """The distinction the whole design turns on.

    Suppressing the TURN would throw away the caller's words. Only the
    interruption is suppressed: `on_user_turn_started` still fires, so the
    aggregation, the transcript and the eventual answer are all unaffected.
    """
    import time

    gate = BargeInGate(ON)
    wrapper, recorder = _wire(gate)

    await wrapper.process_frame(BotStartedSpeakingFrame())
    await wrapper.process_frame(
        VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time())
    )
    await wrapper.process_frame(_interim("ఆ"))

    assert len(recorder.params) == 1, "the user turn must still start"
    assert recorder.params[0].enable_interruptions is False
    assert recorder.params[0].enable_user_speaking_frames is True


async def test_a_real_interruption_reaches_the_aggregator_untouched():
    import time

    gate = BargeInGate(ON)
    wrapper, recorder = _wire(gate)

    await wrapper.process_frame(BotStartedSpeakingFrame())
    await wrapper.process_frame(
        VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time())
    )
    await wrapper.process_frame(_interim("ఆగండి"))

    assert len(recorder.params) == 1
    assert recorder.params[0].enable_interruptions is True


async def test_a_blip_with_no_transcript_is_held_by_duration_alone():
    """The live case: STT has not answered yet when the turn starts.

    `min_words=1` with a one-word interim is what the MB Solar config actually
    does; the word here is not in either lexicon, so only duration can decide.
    """
    import time

    gate = BargeInGate(ON)
    wrapper, recorder = _wire(gate)

    await wrapper.process_frame(BotStartedSpeakingFrame())
    now = time.time()
    await wrapper.process_frame(
        VADUserStartedSpeakingFrame(start_secs=0.05, timestamp=now - 0.10)
    )
    await wrapper.process_frame(_interim("ఇంకా"))

    assert recorder.params[0].enable_interruptions is False


async def test_the_same_word_after_900ms_of_speech_does_interrupt():
    import time

    gate = BargeInGate(ON)
    wrapper, recorder = _wire(gate)

    await wrapper.process_frame(BotStartedSpeakingFrame())
    now = time.time()
    await wrapper.process_frame(
        VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=now - 0.70)
    )
    await wrapper.process_frame(_interim("ఇంకా"))

    assert recorder.params[0].enable_interruptions is True


async def test_the_vad_onset_is_backdated_by_start_secs():
    """VAD commits late; the onset is `timestamp - start_secs`.

    Using arrival time instead would under-count every utterance by 200 ms and
    make `barge_in_min_speech_secs` mean something other than what it says.
    """
    gate = BargeInGate(ON)
    wrapper, _ = _wire(gate)
    wrapper.observe(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=1000.0))
    assert abs(gate.speech_secs(now=1000.0) - 0.2) < 1e-6


async def test_the_wrapper_is_transparent_when_the_gate_allows():
    """Wrapping must not change WHEN the inner strategy fires, only whether
    that firing also stops the bot."""
    import time

    plain = MinWordsUserTurnStartStrategy(min_words=1)
    plain_recorder = _attach(plain)

    gate = BargeInGate(ON)
    wrapper, wrapped_recorder = _wire(gate)

    frames = [
        BotStartedSpeakingFrame(),
        VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time() - 2.0),
        _interim("ఇంకా చెప్పలేదు"),
    ]
    plain_results = [await plain.process_frame(f) for f in frames]
    wrapped_results = [await wrapper.process_frame(f) for f in frames]

    assert plain_results == wrapped_results
    assert len(plain_recorder.params) == len(wrapped_recorder.params) == 1
    assert wrapped_recorder.params[0].enable_interruptions is True


async def test_a_broken_gate_fails_open():
    """A gate that errored closed would make the agent un-interruptible.

    `BaseObject._run_handler` swallows handler exceptions, so an unguarded
    raise in here would have failed silently -- the caller would simply find
    they could never stop the bot, and nothing would log.
    """
    import time

    class _Exploding(BargeInGate):
        def _decide(self, now):
            raise RuntimeError("boom")

    gate = _Exploding(ON)
    wrapper, recorder = _wire(gate)
    await wrapper.process_frame(BotStartedSpeakingFrame())
    await wrapper.process_frame(
        VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time())
    )
    await wrapper.process_frame(_interim("ఆ"))
    assert recorder.params[0].enable_interruptions is True


async def test_a_strategy_that_never_wanted_interruptions_still_does_not_get_them():
    """The gate can only ever subtract. It must not enable an interruption a
    strategy had explicitly turned off -- that is how the realtime providers
    hand barge-in to the model."""
    import time

    inner = MinWordsUserTurnStartStrategy(min_words=1, enable_interruptions=False)
    gate = BargeInGate(BargeInParams(enabled=True))  # allows everything
    wrapper = BargeInGatedUserTurnStartStrategy(inner, gate)
    recorder = _attach(wrapper)

    await wrapper.process_frame(BotStartedSpeakingFrame())
    await wrapper.process_frame(
        VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time() - 2.0)
    )
    await wrapper.process_frame(_interim("ఇంకా"))
    assert recorder.params[0].enable_interruptions is False


# --- upstream canary --------------------------------------------------------


def test_the_lever_this_gate_pulls_still_exists_upstream():
    """`enable_interruptions` is the flag the aggregator reads. If a pipecat
    bump renames or drops it, this gate becomes a no-op that still logs
    "SUPPRESSED" -- the worst possible failure. A source check, deliberately,
    because it is guarding against upstream drift and not against our own
    logic; every other test in this file is behavioural.
    """
    import pipecat.processors.aggregators.llm_response_universal as agg

    src = inspect.getsource(agg)
    assert "if params.enable_interruptions:" in src
    assert "await self.broadcast_interruption()" in src
    assert "enable_interruptions" in inspect.signature(
        UserTurnStartedParams.__init__
    ).parameters
    assert issubclass(BargeInGatedUserTurnStartStrategy, BaseUserTurnStartStrategy)
    assert Path(agg.__file__).exists()
