"""The run 780 duplicate, in the shapes the first six tests do not reach.

`test_realtime_stt_one_turn_per_utterance.py` locked the headline case: Sarvam
re-sends a `final` per stabilised SEGMENT, each was pushed as a finalized
`TranscriptionFrame` -- a whole TURN downstream -- and run 780's user transcript
is the word "హలో" twenty-two times for a caller who said it once. The LLM then
spent 3.668s on that turn against 0.351s on the previous call, and the model was
reverted within the hour.

Those six tests all compare a final against the one immediately before it, which
is exactly as far back as the guard could see. This file covers what happens
one message further out, and the two other ways the service can go quiet:

  * a re-score that goes A -> B -> A, where "A" is not a substring of "B" and
    so walked straight through the guard as a second turn;
  * a re-score that arrives AFTER speech end -- which is not an edge case for
    this model, it is the normal case, since its finals landing 0.09-0.12s after
    the caller stops is the entire 0.25s the switch is for. The guard used to be
    cleared on `UserStoppedSpeakingFrame`, so that ordinary arrival was re-armed
    as a duplicate turn on every turn of every call;
  * a socket that never came up, which used to make the agent deaf for the whole
    call while the greeting, LLM, TTS and dashboard all looked healthy -- the
    same silent failure the module already makes loud for quota errors.

None of this was reachable in production: the service has never been switched
on since the revert.
"""

from __future__ import annotations

import asyncio

import pytest

from pipecat.frames.frames import (
    ErrorFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)

from api.services.vaani import sarvam_realtime_stt
from api.services.vaani.sarvam_realtime_stt import SarvamRealtimeSTTService


class _Recorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame, *a, **kw):
        self.frames.append(frame)


def _service() -> SarvamRealtimeSTTService:
    svc = SarvamRealtimeSTTService(api_key="test", language="te-IN")
    svc.push_frame = _Recorder()
    return svc


def _finals(svc) -> list[str]:
    return [f.text for f in svc.push_frame.frames
            if isinstance(f, TranscriptionFrame)]


# --- looking back further than one message ----------------------------------

@pytest.mark.asyncio
async def test_a_rescore_that_goes_there_and_back_is_not_two_turns():
    """A -> B -> A. The run 780 duplicate with one segment in between.

    The guard compared only against the PREVIOUS final, so by the time "హలో"
    came round again the thing it was being measured against was "బాగున్నారా" --
    which does not contain it. Emitted as a second turn, and the caller said it
    once.
    """
    svc = _service()
    await svc._handle({"event": "final", "text": "హలో"})
    await svc._handle({"event": "final", "text": "బాగున్నారా"})
    await svc._handle({"event": "final", "text": "హలో"})

    assert _finals(svc) == ["హలో", "బాగున్నారా"]


@pytest.mark.asyncio
async def test_the_whole_utterance_is_remembered_not_just_the_last_two():
    """Run 780 was twenty-two copies, not two. Depth matters."""
    svc = _service()
    for text in ["హలో", "హలో సార్", "నమస్తే", "హలో", "హలో సార్", "నమస్తే"]:
        await svc._handle({"event": "final", "text": text})

    # "హలో సార్" extends "హలో", so only "సార్" goes down the pipe -- see
    # `test_a_cumulative_rescore_does_not_reach_the_llm_as_a_stutter`.
    assert _finals(svc) == ["హలో", "సార్", "నమస్తే"]


@pytest.mark.asyncio
async def test_a_final_that_shrinks_is_the_decoder_going_backwards():
    """Finals are not monotonic -- the module docstring records the decoder
    reading "అవును అవును అవును" and then re-reading the same audio. A shorter
    re-score of what was already said adds no words, so it cannot be a turn."""
    svc = _service()
    await svc._handle({"event": "final", "text": "నా పేరు రమేష్"})
    await svc._handle({"event": "final", "text": "నా పేరు"})

    assert _finals(svc) == ["నా పేరు రమేష్"]


@pytest.mark.asyncio
async def test_growth_after_a_shrink_still_reaches_the_aggregator():
    """The guard must suppress repeats without deafening the service: after the
    decoder goes backwards, genuinely new words still have to get through, or
    the LLM answers a fragment."""
    svc = _service()
    await svc._handle({"event": "final", "text": "నా పేరు రమేష్"})
    await svc._handle({"event": "final", "text": "నా పేరు"})
    await svc._handle({"event": "final", "text": "నా పేరు రమేష్ గారు"})

    assert _finals(svc) == ["నా పేరు రమేష్", "గారు"]
    assert " ".join(_finals(svc)) == "నా పేరు రమేష్ గారు", (
        "the aggregator joins these -- the join is what the LLM reads"
    )


@pytest.mark.asyncio
async def test_a_cumulative_rescore_does_not_reach_the_llm_as_a_stutter():
    """The aggregator CONCATENATES; it does not replace.

    `LLMUserAggregator._handle_transcription` appends every TranscriptionFrame of
    a turn to `_aggregation` and joins them when the turn ends. So a cumulative
    re-score pushed whole -- "ఒక", "ఒక లక్ష", "ఒక లక్ష యాభై" -- arrives at the LLM
    as "ఒక ఒక లక్ష ఒక లక్ష యాభై". That is run 780's duplication at word scale
    rather than turn scale: quieter, still wrong, and on a subsidy-linked product
    the words being stuttered are amounts.
    """
    svc = _service()
    for text in ["ఒక", "ఒక లక్ష", "ఒక లక్ష యాభై"]:
        await svc._handle({"event": "final", "text": text})

    assert " ".join(_finals(svc)) == "ఒక లక్ష యాభై"


# --- the turn boundary ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rescore_arriving_after_speech_end_is_still_a_duplicate():
    """The reason to want this model is that its finals land AFTER speech end.

    0.09-0.12s after, against saarika:v2.5's 0.373s -- that lag IS the 0.25s the
    switch buys. So the ordinary live sequence is

        final "హలో" ... UserStoppedSpeaking ... re-scored final "హలో"

    and the guard used to be cleared on the stop frame, which armed the second
    copy as a second turn. On EVERY turn of every call, not as an edge case.

    `PartialResponder` does not cover this either: it suppresses a late final
    only when it promoted a partial, and here a real final had already arrived,
    so it stands aside and the duplicate reaches the aggregator.
    """
    svc = _service()
    await svc._handle({"event": "final", "text": "హలో"})
    await svc.process_frame(UserStoppedSpeakingFrame(), None)
    await svc._handle({"event": "final", "text": "హలో"})

    assert _finals(svc) == ["హలో"], "speech end is not the end of the re-scoring"


@pytest.mark.asyncio
async def test_the_new_utterance_forgets_the_whole_old_one():
    """The guard now remembers every final of the utterance, so the reset has to
    clear all of them -- not just the most recent. A caller who says "హలో" on
    turn 1 and again on turn 5 said it twice."""
    svc = _service()
    await svc._handle({"event": "final", "text": "హలో"})
    await svc._handle({"event": "final", "text": "బాగున్నారా"})
    await svc.process_frame(UserStartedSpeakingFrame(), None)
    await svc._handle({"event": "final", "text": "హలో"})

    assert _finals(svc) == ["హలో", "బాగున్నారా", "హలో"]


@pytest.mark.asyncio
async def test_a_contradicting_rescore_is_emitted_and_that_is_deliberate():
    """The residual risk, written down so a future change to it is a decision.

    The module docstring records a measured pair where the partial read
    "అవును అవును అవును" (yes yes yes) and the final read
    "నో నో నో ఐ డోంట్ హావ్" (no, I do not have). Nothing in a text comparison can
    tell that apart from a genuine second segment, so it is emitted, and the
    aggregator ends the turn holding both. The alternative -- suppressing any
    final that is not an extension of the last -- would silently drop real
    speech, which is the failure this agent cannot afford.
    """
    svc = _service()
    await svc._handle({"event": "final", "text": "అవును"})
    await svc._handle({"event": "final", "text": "నో"})

    assert _finals(svc) == ["అవును", "నో"]


# --- the socket -------------------------------------------------------------

class _DeadSocket(Exception):
    pass


class _connects_to:
    """Swap `websockets.connect` for the duration of one test and put it back.

    `sarvam_realtime_stt` does `import websockets`, so the patch lands on the
    real module and would leak into every other test in the process if it were
    not restored.
    """

    def __init__(self, fn):
        self._fn = fn

    def __enter__(self):
        self._original = sarvam_realtime_stt.websockets.connect
        sarvam_realtime_stt.websockets.connect = self._fn

    def __exit__(self, *exc):
        sarvam_realtime_stt.websockets.connect = self._original
        return False


def _no_tasks(svc):
    """Neuter the task plumbing: these tests are about the socket, and
    `create_task` needs a running pipeline's task manager."""
    created = []

    def create_task(coro):
        coro.close()
        created.append(coro)
        return object()

    async def cancel_task(task, timeout=None):
        return None

    svc.create_task = create_task
    svc.cancel_task = cancel_task
    return created


@pytest.mark.asyncio
async def test_a_refused_connection_is_retried_instead_of_going_deaf():
    """One failure at call setup used to be deafness for the whole call.

    `start()` connects once and swallows the failure by design. `run_stt` then
    returned early on every later chunk without ever retrying, so `_ws` stayed
    None to the end of the call -- and every other part of the call (greeting,
    LLM, TTS, dashboard) kept looking healthy while the agent heard nothing.
    """
    svc = _service()
    _no_tasks(svc)
    attempts = []

    async def refuse(*a, **kw):
        attempts.append(1)
        raise _DeadSocket("refused")

    with _connects_to(refuse):
        async for _ in svc.run_stt(b"\x00\x00"):
            pass
    assert attempts, "a dead socket must be retried, not accepted"


@pytest.mark.asyncio
async def test_the_retry_is_rate_limited():
    """Audio arrives every 20ms. Retrying on each chunk would turn a Sarvam
    outage into a flood against an endpoint that is already refusing us."""
    svc = _service()
    _no_tasks(svc)
    attempts = []

    async def refuse(*a, **kw):
        attempts.append(1)
        raise _DeadSocket("refused")

    with _connects_to(refuse):
        for _ in range(50):
            async for _ in svc.run_stt(b"\x00\x00"):
                pass

    assert len(attempts) == 1, f"50 chunks caused {len(attempts)} connect attempts"


@pytest.mark.asyncio
async def test_a_reconnect_mid_utterance_does_not_forget_what_was_said():
    """The socket restarts; the caller does not.

    A reconnect gives Sarvam a fresh decoder, which re-sends the finals it has
    already stabilised. Resetting the guard there would replay the whole
    utterance into the context as new turns -- run 780 by another route. The
    boundary is the next `UserStartedSpeakingFrame`, and a dropped socket is
    not one.
    """
    svc = _service()
    _no_tasks(svc)

    class _Sock:
        def __init__(self, fail):
            self._fail = fail

        async def send(self, _):
            if self._fail:
                raise _DeadSocket("gone")

    async def connect(*a, **kw):
        return _Sock(fail=False)

    await svc._handle({"event": "final", "text": "హలో"})
    svc._ws = _Sock(fail=True)

    with _connects_to(connect):
        frames = [f async for f in svc.run_stt(b"\x00\x00")]
    assert any(isinstance(f, ErrorFrame) for f in frames), "a dropped socket is reported"
    assert svc._ws is not None, "and re-established"

    await svc._handle({"event": "final", "text": "హలో"})
    assert _finals(svc) == ["హలో"], "the reconnect must not replay the utterance"


@pytest.mark.asyncio
async def test_a_reconnect_does_not_leave_the_old_receive_task_running():
    """Two readers on two sockets would double every transcript, and the older
    handle is overwritten, so `_disconnect` could never cancel it -- the task
    would outlive the call."""
    svc = _service()
    cancelled = []

    def create_task(coro):
        coro.close()
        return f"task{len(cancelled)}"

    async def cancel_task(task, timeout=None):
        cancelled.append(task)

    svc.create_task = create_task
    svc.cancel_task = cancel_task

    class _Sock:
        pass

    async def connect(*a, **kw):
        return _Sock()

    with _connects_to(connect):
        await svc._connect()
        svc._ws = None
        await svc._connect()

    assert cancelled, "the previous receive task must be cancelled on reconnect"


if __name__ == "__main__":  # pragma: no cover
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            asyncio.run(fn())
            print("PASS", name)
