"""The filler must never be the thing that interrupts a caller.

Run 262 measured 0.921s of endpoint+STT against 0.290s of LLM: two thirds of
every gap is silence before the turn is even declared over. A filler covers that
silence -- but only if it can be trusted not to land on top of someone who was
just drawing breath, which this project caps at 2% and which Telugu callers on
the incumbent already complained about at 350ms.

So the tests that matter here are the negative ones: every uncertain path must
produce silence.
"""

from __future__ import annotations

import numpy as np
import pytest

from api.services.vaani import fillers as bank
from api.services.vaani.filler_player import FillerPlayer, FillerState
from pipecat.processors.frame_processor import FrameDirection

SR = 8000


class Analyzer:
    def __init__(self, p): self._last_probability = p


def player(monkeypatch, *, p=0.99, clips=2, **kw) -> tuple[FillerPlayer, list]:
    pcm = (np.zeros(int(0.3 * SR), dtype=np.int16) + 100).tobytes()
    monkeypatch.setattr(bank, "load_cached",
                        lambda t, v, s, _c=[0]: pcm if _c.append(1) or len(_c) <= clips else None)
    fp = FillerPlayer(state=FillerState(), turn_analyzer=Analyzer(p),
                      sample_rate=SR, **kw)
    pushed: list = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)
    monkeypatch.setattr(fp, "push_frame", capture)
    monkeypatch.setattr(FillerPlayer, "__bases__"[0] if False else "_noop", None, raising=False)
    return fp, pushed


async def drive(fp, frames):
    for f in frames:
        # Bypass the base class's link/state machinery; this test exercises this
        # processor's decision, not pipecat's plumbing.
        await FillerPlayer.process_frame.__wrapped__(fp, f, FrameDirection.DOWNSTREAM) \
            if hasattr(FillerPlayer.process_frame, "__wrapped__") else None


def audio(pushed) -> int:
    return sum(1 for f in pushed if isinstance(f, OutputAudioRawFrame))


# --- the safety properties ---------------------------------------------------


def test_silent_when_no_clips_are_rendered(monkeypatch):
    monkeypatch.setattr(bank, "load_cached", lambda *a, **k: None)
    fp = FillerPlayer(state=FillerState(), turn_analyzer=Analyzer(0.99))
    assert fp.active is False, "with no audio there is nothing to play"


def test_silent_when_the_detector_is_unsure(monkeypatch):
    fp, _ = player(monkeypatch, p=0.50)
    assert fp._confident() is False


def test_silent_when_the_detector_never_scored_the_turn(monkeypatch):
    fp, _ = player(monkeypatch, p=None)
    assert fp._confident() is False, "no evidence must mean no filler"


def test_silent_when_there_is_no_detector_at_all(monkeypatch):
    pcm = b"\x00\x01" * 1000
    monkeypatch.setattr(bank, "load_cached", lambda *a, **k: pcm)
    fp = FillerPlayer(state=FillerState(), turn_analyzer=None)
    assert fp._confident() is False


def test_the_gate_sits_above_the_turn_ending_threshold(monkeypatch):
    """Ending a turn early is recoverable. Talking over someone is not."""
    from api.services.vaani.filler_player import DEFAULT_CONFIDENCE
    assert DEFAULT_CONFIDENCE > 0.84


def test_disabled_player_is_inert(monkeypatch):
    fp, _ = player(monkeypatch, enabled=False)
    assert fp.active is False


# --- the bank ----------------------------------------------------------------


def test_every_filler_is_short():
    """This is audio the caller sits through on every gated turn."""
    for text in bank.FILLERS:
        assert len(text) <= 14, f"{text!r} is too long to be a filler"


def test_fillers_are_telugu():
    for text in bank.FILLERS:
        assert any("ఀ" <= ch <= "౿" for ch in text), text


def test_cache_key_separates_voices():
    """A clip in the wrong voice is worse than no clip: two people on one call."""
    a = bank.cache_path("సరే", "anushka", 8000)
    b = bank.cache_path("సరే", "meera", 8000)
    assert a != b


def test_cache_key_separates_sample_rates():
    assert bank.cache_path("సరే", "anushka", 8000) != bank.cache_path("సరే", "anushka", 16000)


def test_a_truncated_clip_is_rejected(tmp_path, monkeypatch):
    """A clipped file is a click in the caller's ear."""
    monkeypatch.setattr(bank, "CACHE_DIR", tmp_path)
    p = bank.cache_path("సరే", "anushka", SR)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00\x01" * 10)          # ~2.5ms
    assert bank.load_cached("సరే", "anushka", SR) is None


# --- not saying the same word twice ------------------------------------------


@pytest.mark.parametrize("reply,expected", [
    ("సరే, మీ నెలవారీ బిల్లు ఎంత?", "మీ నెలవారీ బిల్లు ఎంత?"),
    ("అర్థమైంది సార్, మీ బిల్లు 10 లక్షలు.", "మీ బిల్లు 10 లక్షలు."),
    ("మంచిది సుబ్బరాజు, షెడ్యూల్ చేద్దామా?", "సుబ్బరాజు, షెడ్యూల్ చేద్దామా?"),
    ("మీ పేరు ఏమిటి?", "మీ పేరు ఏమిటి?"),
])
def test_the_reply_does_not_repeat_the_filler_word(reply, expected):
    assert bank.strip_leading_ack(reply) == expected


def test_an_acknowledgement_only_reply_survives():
    """Stripping must never leave the caller with silence."""
    assert bank.strip_leading_ack("సరే").strip() != ""


def test_the_flag_is_consumed_once():
    s = FillerState()
    s.played_this_turn = True
    assert s.consume() is True
    assert s.consume() is False, "only the first chunk of a reply may be trimmed"
