"""The filler player must have audio for the voice that is actually on the call.

Every piece of this feature was built, wired into the pipeline, gated on the
turn detector, and covered by tests -- and it had never once made a sound.

`FillerPlayer._load` asks `fillers.load_cached` for a clip per filler, keyed by
the LIVE tts voice. There was no clip for that voice, so `_clips` was empty,
`active` was False, and the player returned silence on every call ever placed.
Nothing failed. The log line said "Filler player inert" and no test asserted
otherwise, because every existing test constructs the player with a voice of its
own choosing and then checks the logic around it.

So after the caller finished speaking there was 0.5-1.0s of dead air before a
fully formed sentence arrived. That is the client's "everything is robotic",
6 Sep, and it was never a prompt problem.

Two ways it was silent, both fixed and both pinned here:

  * `render_fillers.py` only spoke to Sarvam, while the pipeline has run
    Cartesia since the cutover. Clips could not be keyed to the live voice.
  * `load_cached` preferred clips under the bare key "harvested", which any
    voice could reach. Three clips cut on 28 Aug were still winning after the
    voice was rotated on 5 Sep -- the caller would have heard the previous
    speaker on half the fillers.

This test hardcodes the live voice ON PURPOSE. If the voice is rotated again and
the clips are not re-rendered, this must fail loudly rather than the agent going
quietly back to dead air. Re-render with:

    python tools/render_fillers.py --provider cartesia --voice <new id>
"""

from __future__ import annotations

import pytest

from api.services.vaani import fillers as filler_bank
from api.services.vaani.filler_player import FillerPlayer, FillerState

# The live Cartesia voice, from `tools/set_stt_config.py --section tts`.
# Not a secret; the API key beside it is, and is not here.
LIVE_VOICE = "6053be8e-db2c-4e7d-879f-0bcf75580122"
TRANSPORT_SR = 8000


def _player(voice: str) -> FillerPlayer:
    return FillerPlayer(state=FillerState(), voice=voice,
                        sample_rate=TRANSPORT_SR, enabled=True)


def test_the_live_voice_has_rendered_clips():
    player = _player(LIVE_VOICE)
    assert player.active, (
        f"no filler audio for the live voice {LIVE_VOICE!r} at {TRANSPORT_SR}Hz "
        "-- the agent plays dead air into every gap. Re-render with "
        "tools/render_fillers.py --provider cartesia --voice <id>"
    )
    assert len(player._clips) >= 4


def test_the_pipeline_lowercases_the_voice_so_the_lookup_must_too():
    """run_pipeline passes `(...voice or "anushka").lower()`.

    A cache keyed on the mixed-case id would miss at runtime and the failure
    would be silence -- indistinguishable from the bug this file exists for.
    """
    assert _player(LIVE_VOICE.upper()).active


def test_an_unrendered_voice_is_silent_rather_than_wrong():
    """The failure mode must stay 'no filler', never 'somebody else's voice'."""
    assert not _player("a-voice-nobody-has-rendered").active


def test_a_stale_harvested_clip_cannot_reach_a_different_voice():
    """The 5 Sep rotation, pinned.

    Clips harvested under one voice must become unreachable when the voice
    changes, by construction rather than by remembering to delete them.
    """
    assert filler_bank.harvested_key("voice-a") != filler_bank.harvested_key("voice-b")
    assert LIVE_VOICE in filler_bank.harvested_key(LIVE_VOICE)


@pytest.mark.parametrize("text", filler_bank.FILLERS)
def test_every_filler_is_a_continuation_and_asserts_nothing(text):
    """A filler is chosen BEFORE the model reads the turn, so it cannot agree.

    "అవును" (yes) was in this bank. Asked "సబ్సిడీ వస్తుందా?" the agent would
    have answered yes out of a cache, ahead of any check -- an invented
    commitment on a sales call, which is what most of the guardrails in this
    codebase exist to prevent.
    """
    assert text not in ("అవును", "కాదు", "లేదు", "ఉంది")


def test_no_clip_is_long_enough_to_become_the_latency_it_hides():
    """A cover can delay the real reply by at most LEAD_MS, by design.

    Pinned because the constant is the entire latency argument for this feature:
    it removes 0.5-1.0s of dead air and can add at most 60ms.
    """
    from api.services.vaani.filler_player import LEAD_MS
    assert LEAD_MS <= 100
