"""The analyzer must know whether its transcript describes the turn it is judging.

Measured 6 Sep across 100 recordings and 589 speech bursts, at the moment the
endpoint decision is taken:

    text for THIS burst had arrived   31.2%
    only OLDER text available         44.7%
    no text at all yet                24.1%

So `sounds_unfinished(self._text)` -- the entire text half of the decision -- is
reading the wrong sentence on more than two thirds of turns. `_clear()` wipes
`_text` when a turn ends, and STT delivers the transcript some hundreds of
milliseconds later, so whatever is sitting there mid-turn arrived AFTER the
previous turn closed. It is the previous utterance.

The error is not symmetric. Text from a FINISHED earlier sentence does not read
as unfinished, so it leaves the early-end path open -- it does not merely fail to
help, it pushes toward cutting the caller off.

`blind_min_silence_ms` defaults to 0.0 and changes nothing. It is a measured
lever, not a shipped one: on the same 589 bursts it trades cut-offs for wait
almost exactly one for one (33.3% at 0.30s -> 24.1% at 0.78s), which is not a
fix. Pinned at 0.0 here so it cannot be switched on without that being a
deliberate, reviewed decision.
"""

from __future__ import annotations

from api.services.vaani.telugu_turn import TeluguTurnAnalyzer, TeluguTurnParams


def _a() -> TeluguTurnAnalyzer:
    return TeluguTurnAnalyzer(sample_rate=8000, params=TeluguTurnParams())


def test_an_analyzer_that_has_heard_nothing_is_blind():
    assert not _a().text_is_fresh


def test_a_transcript_that_has_just_arrived_is_fresh():
    a = _a()
    a.note_text("మా నెల బిల్లు రెండు లక్షలు")
    assert a.text_is_fresh


def test_a_transcript_from_the_PREVIOUS_turn_is_not_fresh():
    """The 44.7% case, and the one that was invisible."""
    a = _a()
    a.note_text("మా నెల బిల్లు రెండు లక్షలు")
    assert a.text_is_fresh

    # End the turn the way append_audio does, then start a new one.
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
    a._clear(EndOfTurnState.COMPLETE)
    a._speech_triggered = False
    a._turn_id += 1                      # the caller starts speaking again

    assert not a.text_is_fresh, (
        "text left over from the last utterance was being read as evidence "
        "about this one"
    )


def test_the_blind_floor_is_off_by_default():
    assert TeluguTurnParams().blind_min_silence_ms == 0.0
