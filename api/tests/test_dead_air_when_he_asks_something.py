"""Dead air: he asks something, hears nothing, and says "హలో".

Runs 881 and 882, after the mid-flight staleness rule shipped earlier the same
day. The tell is his own word:

    USER: ...కాస్ట్ ఎంత వేసింది మామూలుగా? 60 టు 60 స్క్వేర్ ఫీట్ అయితే
    USER: హలో.                                  <- nothing came back

`fdabd98` widened `_stale`: a reply is dropped when the caller takes the floor
mid-generation and nothing has been spoken yet. That is right for run 865, where
two real utterances produced two replies and both were spoken. It was keyed on
`UserStartedSpeakingFrame` -- bare VAD -- and that is the mistake.

Two ways bare VAD fires without the caller saying anything new:

  1. ECHO. Run 881 is the proof, in the transcript itself: the agent's own
     greeting came back as USER text, word for word. `echoes_agent` already
     drops echoed TEXT before triage, but nothing drops the VAD event it
     arrives with. So the agent's own voice marked its own reply stale.
  2. Line noise, a breath, a cough.

And a dropped reply is not retried. Nothing regenerates, because a new reply
needs a new turn and bare VAD does not complete one. The caller is left in
silence until he speaks again -- which is exactly what "హలో" is.

So staleness now needs EVIDENCE: a real, non-echo transcription arriving while
the reply is still being built. That is self-healing by construction, because
the same transcription is what starts the next turn -- so the reply that
replaces the dropped one is already on its way.
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.state import CallState

REPLY = "సైట్ సర్వే ఖర్చు మీ రూఫ్ సైజ్ మీద ఆధారపడి ఉంటుంది అండి."


class _Injector:
    def __init__(self, state):
        self.state = state


def _filter(state=None):
    st = state or CallState(required_fields=["location"],
                            questions={"location": "ఏ ఏరియా?"})
    rf = ReplyFilter(_Injector(st))
    rf.spoken_out = []

    async def _collect(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, LLMTextFrame):
            rf.spoken_out.append(frame.text)

    rf.push_frame = _collect
    return rf


async def _send(rf, frame):
    await rf.process_frame(frame, FrameDirection.DOWNSTREAM)


def _said(rf) -> str:
    return "".join(rf.spoken_out)


@pytest.mark.asyncio
async def test_bare_voice_detection_does_not_create_dead_air():
    """The regression. A blip of VAD is not the caller saying something."""
    rf = _filter()
    await _send(rf, LLMFullResponseStartFrame())
    await _send(rf, UserStartedSpeakingFrame())      # echo, noise, a breath
    await _send(rf, LLMTextFrame(REPLY))
    await _send(rf, LLMFullResponseEndFrame())
    assert REPLY in _said(rf), (
        "nothing was transcribed, so nothing will start a new turn -- dropping "
        "this reply leaves the caller in silence until he speaks again, which "
        "is the 'హలో' in runs 881 and 882")


@pytest.mark.asyncio
async def test_the_agents_own_echo_does_not_create_dead_air():
    """Run 881: the agent's greeting came back as the caller's words."""
    greeting = "మీరు సోలార్ గురించి ఎంక్వైరీ చేశారు కదా, ఒక్క నిమిషం మాట్లాడవచ్చా?"
    st = CallState(required_fields=["location"], questions={"location": "ఏ ఏరియా?"})
    st.asked = [greeting[:90]]
    rf = _filter(st)

    await _send(rf, LLMFullResponseStartFrame())
    await _send(rf, UserStartedSpeakingFrame())
    await _send(rf, TranscriptionFrame(greeting, "user", "t0"))
    await _send(rf, LLMTextFrame(REPLY))
    await _send(rf, LLMFullResponseEndFrame())
    assert REPLY in _said(rf), (
        "the agent heard ITSELF. Letting that silence the reply is run 881, "
        "where the call filled with the agent answering its own questions")


@pytest.mark.asyncio
async def test_a_real_interruption_still_drops_the_reply():
    """Run 865 must not regress: two real utterances, one reply."""
    rf = _filter()
    await _send(rf, LLMFullResponseStartFrame())
    await _send(rf, UserStartedSpeakingFrame())
    await _send(rf, TranscriptionFrame("ఆగండి, నేను చెప్తున్నాను", "user", "t0"))
    await _send(rf, LLMTextFrame(REPLY))
    await _send(rf, LLMFullResponseEndFrame())
    assert _said(rf) == "", (
        "he really did take the floor, and his words will start the next turn, "
        "so the reply that replaces this one is already coming")


@pytest.mark.asyncio
async def test_a_reply_already_being_heard_still_finishes():
    """Never retract audio -- run 783's truncation."""
    rf = _filter()
    await _send(rf, LLMFullResponseStartFrame())
    await _send(rf, LLMTextFrame("మేము వెరిఫైడ్ వెండర్లను కనెక్ట్ చేస్తాం అండి."))
    already = _said(rf)
    assert already
    await _send(rf, UserStartedSpeakingFrame())
    await _send(rf, TranscriptionFrame("ఆగండి", "user", "t1"))
    await _send(rf, LLMTextFrame(" ఉచిత సర్వే కూడా ఉంటుంది."))
    await _send(rf, LLMFullResponseEndFrame())
    assert _said(rf).startswith(already)
    assert "ఉచిత సర్వే" in _said(rf)
