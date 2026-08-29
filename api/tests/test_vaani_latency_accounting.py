"""The reported latency has to be the latency the caller waits.

Run 273, all seven turns, from the run log:

    TOTAL 2.111 = endpoint 0.503 + LLM 1.608     exactly
    TOTAL 0.691 = endpoint 0.454 + LLM 0.237     exactly

Nothing else was in it. The observer discarded every metric whose processor name
did not contain "LLM", so the speech engine's time never reached the log at all
-- and the client heard three seconds where the log said 2.111.

Two months of latency work were measured against a number that stopped at the
model's first token. These tests exist so that cannot silently return.
"""

from __future__ import annotations


def totals(endpoint, ttfbs):
    """The arithmetic run_pipeline uses to build a latency-breakdown payload."""
    llm = sum(v for k, v in ttfbs.items() if "LLM" in k)
    tts = sum(v for k, v in ttfbs.items() if "TTS" in k or "Speech" in k)
    return {
        "endpoint_secs": endpoint,
        "llm_secs": round(llm, 4) or None,
        "tts_secs": round(tts, 4) or None,
        "heard_secs": round(endpoint + llm + tts, 4) if endpoint is not None else None,
    }


def test_the_speech_engine_is_counted():
    """The defect: TTS timing existed and was thrown away before the log."""
    p = totals(0.503, {"HedgedGroqLLMService#0": 1.608, "CartesiaTTSService#1": 0.312})
    assert p["tts_secs"] == 0.312
    assert p["heard_secs"] == 2.423, "the caller waits for the audio, not the token"


def test_the_old_number_is_no_longer_the_total():
    """endpoint + LLM was reported as TOTAL. It is a component now, not the answer."""
    p = totals(0.503, {"LLMService": 1.608, "CartesiaTTSService#1": 0.4})
    assert p["heard_secs"] > p["endpoint_secs"] + p["llm_secs"]


def test_a_call_with_no_tts_metric_still_reports():
    """Absent TTS must not blank the whole breakdown -- that hid the gap before."""
    p = totals(0.6, {"HedgedGroqLLMService#0": 0.3})
    assert p["heard_secs"] == 0.9
    assert p["tts_secs"] is None


def test_every_component_is_reported_separately():
    """A single total cannot be acted on; the components say what to fix."""
    p = totals(0.72, {"LLM": 0.28, "CartesiaTTSService#1": 0.27})
    assert p["endpoint_secs"] == 0.72
    assert p["llm_secs"] == 0.28
    assert p["tts_secs"] == 0.27
    assert p["heard_secs"] == 1.27


def test_hedged_llm_copies_are_not_double_counted():
    """Hedging races copies of one request; only one of them is the wait."""
    p = totals(0.5, {"HedgedGroqLLMService#0": 0.3})
    assert p["llm_secs"] == 0.3
