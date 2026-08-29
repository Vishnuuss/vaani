"""The realtime STT service: what it does with each message off the socket.

The network half is proven separately and against the real API, by
`tools/probe_sarvam_realtime.py`, which replays this project's own caller
recordings at wall-clock speed. That is where the numbers in the module
docstring come from -- 0.121s / 0.117s / 0.091s speech-end to transcript,
against saarika:v2.5's 0.373s.

What is left, and what these cover, is the routing: which frame each message
becomes. It is worth testing on its own because two of the rules are silent
when they break. A final without `finalized=True` still reaches the LLM and the
call sounds fine -- only the TTFB metric goes wrong, and this project has
already spent a day reading a latency number that was measuring the wrong
thing. And forwarding Sarvam's VAD events would hand turn-taking to a second,
networked opinion that races the trained detector; the calls would simply start
interrupting people again, with nothing in the logs to say why.
"""

import asyncio

import pytest

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
)

from api.services.vaani.sarvam_realtime_stt import (
    MODEL,
    SarvamRealtimeSTTService,
)


class Spy(SarvamRealtimeSTTService):
    """Captures pushed frames instead of sending them down the pipeline."""

    def __init__(self):
        super().__init__(api_key="test", language="te-IN", sample_rate=8000)
        self.frames = []
        self.errors = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    async def push_error(self, error_msg=None, **kwargs):
        self.errors.append(error_msg)


@pytest.fixture
def stt() -> Spy:
    return Spy()


def handle(service: Spy, message: dict) -> None:
    """Drive one message through.

    Plain `asyncio.run` rather than an async test, so these do not depend on a
    pytest plugin being installed -- the speculation suite is currently unable
    to run locally for exactly that reason, and a test that cannot be run is
    not a test.
    """
    asyncio.run(service._handle(message))


# --- the two frames that matter --------------------------------------------

def test_a_partial_becomes_an_interim_frame(stt):
    """The frame preemptive generation needs, and has never once had."""
    handle(stt, {"event": "transcript.partial", "text": "వన్ లాక్"})

    assert len(stt.frames) == 1
    assert isinstance(stt.frames[0], InterimTranscriptionFrame)
    assert stt.frames[0].text == "వన్ లాక్"


def test_a_final_is_marked_finalized(stt):
    """Silent if wrong: the call still works, only the TTFB metric lies.

    `STTService.push_frame` reports TTFB immediately on a finalized transcript
    and otherwise waits out a timeout, so without this the logged STT figure is
    the timeout rather than the measurement.
    """
    handle(stt, {"event": "transcript.final", "text": "ఒక లక్ష"})

    assert len(stt.frames) == 1
    frame = stt.frames[0]
    assert isinstance(frame, TranscriptionFrame)
    assert frame.text == "ఒక లక్ష"
    assert frame.finalized is True


def test_a_partial_is_never_marked_final(stt):
    handle(stt, {"event": "transcript.partial", "text": "ఒక"})
    assert not isinstance(stt.frames[0], TranscriptionFrame)


# --- what must be ignored ---------------------------------------------------

@pytest.mark.parametrize("event", ["vad.speech_start", "vad.speech_end"])
def test_sarvams_own_vad_is_ignored(stt, event):
    """Turn-taking belongs to TeluguTurnAnalyzer and the local Silero VAD.

    A second opinion arriving over the network would race the trained detector
    and undo the two-sided endpointing that stops this agent talking over
    people -- and it would do it silently.
    """
    handle(stt, {"event": event, "utterance_idx": 1, "confidence": 0.97})
    assert stt.frames == []


def test_an_empty_partial_pushes_nothing(stt):
    """The decoder flickers to "" and back while re-scoring -- 3 times in one
    utterance of the probe run. Pushing those would clear text the aggregator
    is holding."""
    handle(stt, {"event": "transcript.partial", "text": ""})
    handle(stt, {"event": "transcript.partial", "text": "   "})
    assert stt.frames == []


def test_an_unknown_event_is_harmless(stt):
    handle(stt, {"event": "something.new", "text": "x"})
    assert stt.frames == []


# --- the failure that must be loud -----------------------------------------

def test_quota_exhaustion_is_surfaced_not_swallowed(stt):
    """The one error that must not be quiet.

    Sarvam credits ran out mid-afternoon on 2026-08-29 and the only symptom
    would have been an agent that hears nothing while every other part of the
    call continues to look healthy.
    """
    handle(stt, {"event": "error", "code": "quota_exceeded",
                       "message": "Credits exhausted."})

    assert stt.errors, "an exhausted account must reach the error path"
    assert "Credits exhausted." in stt.errors[0]
    assert stt.frames == []


# --- configuration ----------------------------------------------------------

def test_the_model_name_reaches_the_metrics(stt):
    """Without it the ttfb metric is unlabelled and the two STT paths cannot be
    told apart in the call log after the fact."""
    assert stt._settings.model == MODEL


def test_it_transcribes_rather_than_translates():
    """The translate modes return English. Every parser in this package --
    amounts, booking, completeness, corrections -- reads Telugu."""
    import inspect

    from api.services.vaani import sarvam_realtime_stt

    source = inspect.getsource(sarvam_realtime_stt.SarvamRealtimeSTTService._connect)
    assert '"mode": "transcribe"' in source
