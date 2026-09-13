"""Sarvam must not add its own end-of-turn wait on top of ours.

The realtime socket runs a server-side VAD whose `silence_duration_ms` is
documented as "silence (ms) marking end-of-turn", default **500**. We never
sent the parameter, so every final transcript was held half a second after the
caller stopped -- and then `SpeechTimeoutUserTurnStopStrategy` waited its own
0.7s on top. The two waits stacked.

Runs 969 and 970 are the measurement:

    saarika:v2.5                 endpoint 0.888s   spread 0.793 - 1.181
    saaras:v3-realtime                    1.789s          1.720 - 1.887
    the same + partial de-dup             1.824s          1.720 - 1.974

    STT metric: 0.654, 0.655, 0.656, 0.655, 0.654, 0.657 ...

A number constant to three decimals across fifteen turns is not a measurement,
it is a fixed wait. Both attempts were reverted without finding it; this is it.

Sending a short `silence_duration_ms` does not hand turn-taking to Sarvam. It
stops Sarvam DELAYING WORDS, which is what this module's header asks of it:
"this service reports words. It does not decide turns."
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from api.services.vaani import sarvam_realtime_stt as srt


@pytest.fixture
def connect_url(monkeypatch):
    """The URL `_connect` builds, captured without raising.

    Raising out of the patched `websockets.connect` does not work: the service
    catches every exception there on purpose, because a refused socket at call
    setup must not take the call down. So the URL is recorded instead.
    """
    seen: list[str] = []

    async def fake_connect(url, **kwargs):
        seen.append(url)
        raise RuntimeError("not connecting in a test")

    monkeypatch.setattr(srt.websockets, "connect", fake_connect)

    async def _go():
        svc = srt.SarvamRealtimeSTTService(api_key="k", language="te-IN",
                                          sample_rate=8000)
        await svc._connect()
        return seen[0] if seen else None
    return _go


@pytest.mark.asyncio
async def test_silence_duration_is_sent_at_all(connect_url):
    """The defect: the parameter was simply absent, so the default 500 applied."""
    url = await connect_url()
    assert url, "connect did not reach websockets.connect"
    q = parse_qs(urlparse(url).query)
    assert "silence_duration_ms" in q, (
        "Sarvam will hold every final for its default 500ms and that wait "
        "stacks on top of our turn timer")


@pytest.mark.asyncio
async def test_it_is_well_under_our_own_turn_timer(connect_url):
    """It must not be a second opinion about when the turn ended.

    `dograh_speech_timeout_secs` is 0.7 on the live agent. Anything close to
    that is a competing endpointer, which is the stacking this fixes.
    """
    q = parse_qs(urlparse(await connect_url()).query)
    ms = int(q["silence_duration_ms"][0])
    assert 0 < ms <= 200, f"{ms}ms is close enough to our timer to stack again"


@pytest.mark.asyncio
async def test_the_transcription_settings_are_unchanged(connect_url):
    """This fix must not quietly change what the caller's words become."""
    q = parse_qs(urlparse(await connect_url()).query)
    assert q["mode"] == ["transcribe"], "translate would return English"
    assert q["language_code"] == ["te-IN"]
    assert q["encoding"] == ["linear16"]
    assert q["sample_rate"] == ["8000"]
