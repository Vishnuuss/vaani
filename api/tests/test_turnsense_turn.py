"""TurnSense analyzer: the mechanics that decide WHEN a turn ends.

A stub model stands in for the trained one so these pin the policy, not the
weights: the words say finished -> end at the text; the words say unfinished ->
wait; no words for this pause -> today's 0.7 s timer, never worse than live.
"""
from __future__ import annotations

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

from api.services.vaani.turnsense import TurnSenseModel, dense_features, ngram_keys
from api.services.vaani.turnsense_turn import TurnSenseAnalyzer, TurnSenseParams

SR = 8000
FRAME = b"\x00\x00" * (SR // 50)        # 20 ms


class Stub:
    """p = whatever the last caller segment maps to; records what it was given."""

    def __init__(self, table: dict[str, float], default: float = 0.5):
        self.table, self.default = table, default
        self.seen: list[tuple[str, str, str]] = []
        self.habit: list[tuple[float, int]] = []

    def probability(self, agent, turn, last, seg_secs=1.0, n_segs=1,
                    caller_hold_rate=0.15, caller_pauses=0):
        self.seen.append((agent, turn, last))
        self.habit.append((caller_hold_rate, caller_pauses))
        return self.table.get(last, self.default)


def make(table=None, **params):
    a = TurnSenseAnalyzer(sample_rate=SR, params=TurnSenseParams(**params),
                          model=Stub(table or {}))
    a.set_sample_rate(SR)
    return a


def speak(a, secs):
    for _ in range(int(round(secs * 50))):
        assert a.append_audio(FRAME, True) == EndOfTurnState.INCOMPLETE


def silence_until_complete(a, limit=5.0):
    """Seconds of silence (after the VAD stop) until COMPLETE, or None."""
    for i in range(int(limit * 50)):
        if a.append_audio(FRAME, False) == EndOfTurnState.COMPLETE:
            return (i + 1) / 50
    return None


def test_no_text_is_todays_timer():
    a = make()
    speak(a, 1.0)
    assert abs(silence_until_complete(a) - 0.70) < 0.021


def test_finished_words_end_the_turn_at_the_text():
    a = make({"నా పేరు సురేష్.": 0.97})
    speak(a, 1.0)
    for _ in range(12):                                  # 0.24 s: STT at work
        assert a.append_audio(FRAME, False) == EndOfTurnState.INCOMPLETE
    a.note_text("నా పేరు సురేష్.")
    assert a.append_audio(FRAME, False) == EndOfTurnState.COMPLETE


def test_unfinished_words_hold_the_floor():
    a = make({"మాది.": 0.05}, max_wait_secs=1.2)
    speak(a, 0.6)
    for _ in range(12):
        a.append_audio(FRAME, False)
    a.note_text("మాది.")
    # would the timer have ended it? at 0.70 s -- this must still be holding
    for _ in range(int((0.70 - 0.24) * 50)):
        assert a.append_audio(FRAME, False) == EndOfTurnState.INCOMPLETE
    # and he carries on: speech resets everything
    speak(a, 1.0)
    assert a.speech_triggered


def test_unfinished_still_ends_at_the_ceiling():
    a = make({"మాది.": 0.0}, max_wait_secs=1.2)
    speak(a, 0.6)
    a.append_audio(FRAME, False)                         # Silero has stopped
    a.note_text("మాది.")
    assert abs((silence_until_complete(a) + 0.02) - 1.2) < 0.021


def test_text_that_lands_while_he_is_talking_is_not_fresh():
    a = make({"మాది.": 0.99})
    speak(a, 0.6)
    a.note_text("మాది.")                  # the finalize raced his next words
    speak(a, 0.2)
    # no fresh verdict for THIS pause -> the timer, not an instant end
    assert abs(silence_until_complete(a) - 0.70) < 0.021


def test_text_before_any_speech_is_ignored():
    a = make({"హలో": 0.99})
    a.note_text("హలో")
    assert a.append_audio(FRAME, False) == EndOfTurnState.INCOMPLETE
    assert not a.speech_triggered


def test_agent_line_and_whole_turn_reach_the_model():
    stub = Stub({"2 లక్షలు వస్తున్నాయి.": 0.95})
    a = TurnSenseAnalyzer(sample_rate=SR, params=TurnSenseParams(), model=stub)
    a.set_sample_rate(SR)
    a.note_agent("మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?")
    speak(a, 0.5)
    a.append_audio(FRAME, False)
    a.note_text("మాది.")
    speak(a, 1.0)
    a.append_audio(FRAME, False)
    a.note_text("2 లక్షలు వస్తున్నాయి.")
    agent, turn, last = stub.seen[-1]
    assert agent.startswith("మీ కరెంట్")
    assert turn == "మాది. 2 లక్షలు వస్తున్నాయి."
    assert last == "2 లక్షలు వస్తున్నాయి."


def test_interpolation_is_monotonic():
    a = make()
    waits = [a.wait_for(p / 20) for p in range(21)]
    assert all(x >= y for x, y in zip(waits, waits[1:]))
    assert a.wait_for(None) == a.params.no_text_wait_secs


def test_missing_model_degrades_to_the_timer(tmp_path):
    missing = TurnSenseModel.load(tmp_path / "nope.json")
    assert missing is None
    a = TurnSenseAnalyzer(sample_rate=SR, model=None)
    a._model = None
    a.enabled = False
    a.set_sample_rate(SR)
    speak(a, 1.0)
    a.note_text("సరే.")
    assert abs(silence_until_complete(a) - 0.70) < 0.021


def test_complete_resets_the_turn():
    a = make({"సరే.": 0.99})
    speak(a, 0.5)
    a.note_text("సరే.")
    assert silence_until_complete(a) is not None
    assert not a.speech_triggered
    speak(a, 0.5)                          # a new turn starts clean
    assert abs(silence_until_complete(a) - 0.70) < 0.021


async def test_analyze_end_of_turn_never_claims_complete_at_the_vad_stop():
    a = make({"సరే.": 0.99})
    speak(a, 0.5)
    state, _ = await a.analyze_end_of_turn()
    assert state == EndOfTurnState.INCOMPLETE


def test_features_are_stable_and_read_the_tail():
    f = dense_features("మీ పేరు ఏంటి?", "నా పేరు సురేష్.", "నా పేరు సురేష్.", 1.2, 1)
    assert f["ask_name"] == 1.0 and f["punct_stop"] == 1.0
    g = dense_features("", "అరవై", "అరవై", 0.4, 1)
    assert g["unfinished_rule"] == 1.0
    keys = ngram_keys("", "సొంత ఇల్లు.", "సొంత ఇల్లు.")
    assert any(k.endswith("⟩") for k in keys)
    assert "w1:ఇల్లు" in keys


def test_the_callers_own_pausing_habit_is_learned_online():
    stub = Stub({}, default=0.5)
    a = TurnSenseAnalyzer(sample_rate=SR, params=TurnSenseParams(), model=stub)
    a.set_sample_rate(SR)
    # three short pauses he talks straight through (0.3 s of silence each)
    for _ in range(3):
        speak(a, 0.5)
        for _ in range(15):
            a.append_audio(FRAME, False)
    speak(a, 0.5)
    a.append_audio(FRAME, False)
    a.note_text("మాది")
    rate, pauses = stub.habit[-1]
    assert pauses == 3
    assert rate > 0.5                    # he is a pauser: be patient with him


def test_the_shipped_model_reads_telugu():
    """The artifact that ships, on the cases this project was built around."""
    m = TurnSenseModel.load()
    assert m is not None, "models/turnsense_te.json must ship with the code"
    name = m.probability("మీ పేరు చెప్పగలరా?", "నా పేరు సురేష్.", "నా పేరు సురేష్.")
    # a name ending on the vowel sign that also ends a non-finite verb ("చేసి")
    # scored 0.85 before `name_given`; it must clear the fast bar on its own
    vowel_end = m.probability("మీ పేరు చెప్పగలరా?", "నా పేరు రవి.", "నా పేరు రవి.")
    bill = m.probability("మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
                         "2 లక్షలు వస్తున్నాయి.", "2 లక్షలు వస్తున్నాయి.")
    dangling = m.probability("మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?", "మాది.", "మాది.")
    cut = m.probability("", "హలో. నేను—", "హలో. నేను—")
    # a finished answer takes the fast path; the run-1021 "మాది." does not
    assert name >= 0.85 and bill >= 0.85 and vowel_end >= 0.85
    assert dangling < 0.6 and cut < 0.6


def test_scoring_costs_nothing_a_caller_could_hear():
    import time
    m = TurnSenseModel.load()
    t0 = time.perf_counter()
    for _ in range(200):
        m.probability("మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
                      "ఆ... మాది. 2 లక్షలు వస్తున్నాయి.", "2 లక్షలు వస్తున్నాయి.",
                      1.2, 2, 0.2, 5)
    per_call = (time.perf_counter() - t0) / 200
    assert per_call < 0.005, f"{per_call * 1000:.2f} ms per decision"
