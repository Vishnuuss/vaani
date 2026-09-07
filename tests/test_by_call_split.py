"""The split must be by CALL, or every number in this programme is inflated.

Two bursts from one call share a speaker, a handset, a room and a noise floor.
Put one in train and the other in test and the model can score well by
recognising the voice, which is not the task. The failure is silent: nothing
crashes, the accuracy simply looks better than it is.

`tools/audio_native_turn.py` and `tools/finetune_audio_turn.py` each carry a
copy of `_by_call_split` and MUST agree, because the audio-native result is
quoted against the prosody baseline measured by the other file. If the two ever
drift apart, the comparison silently stops being a comparison.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _calls(n_calls: int = 40, per_call: int = 5) -> np.ndarray:
    return np.array([f"1_{c}" for c in range(n_calls) for _ in range(per_call)])


def test_no_call_appears_on_both_sides():
    probe = _load("audio_native_turn")
    calls = _calls()
    tr, te = probe._by_call_split(calls)
    assert set(calls[tr]).isdisjoint(set(calls[te])), (
        "a call landed in train AND test; the score would measure speaker "
        "recognition, not turn detection")


def test_every_burst_lands_on_exactly_one_side():
    probe = _load("audio_native_turn")
    calls = _calls()
    tr, te = probe._by_call_split(calls)
    assert (tr ^ te).all(), "a burst was dropped or double-counted"
    assert tr.sum() + te.sum() == len(calls)


def test_the_holdout_is_not_empty_and_not_everything():
    probe = _load("audio_native_turn")
    calls = _calls()
    _, te = probe._by_call_split(calls)
    assert 0 < te.sum() < len(calls)


def test_the_split_is_deterministic():
    """Two runs must produce the same fence, or results are not comparable."""
    probe = _load("audio_native_turn")
    calls = _calls()
    a, _ = probe._by_call_split(calls)
    b, _ = probe._by_call_split(calls)
    assert (a == b).all()


def test_a_different_seed_moves_the_fence():
    probe = _load("audio_native_turn")
    calls = _calls()
    a, _ = probe._by_call_split(calls, seed=0)
    b, _ = probe._by_call_split(calls, seed=7)
    assert not (a == b).all(), "the seed is being ignored"


def test_the_two_tools_split_identically():
    """The audio-native number is quoted against the prosody baseline that the
    OTHER tool measured. Same seed, same fence, or it is not a comparison."""
    probe = _load("audio_native_turn")
    tune = _load("finetune_audio_turn")
    calls = _calls()
    a, _ = probe._by_call_split(calls)
    b, _ = tune._by_call_split(calls)
    assert (a == b).all(), (
        "audio_native_turn and finetune_audio_turn disagree on the split; "
        "the fine-tuned score is no longer comparable to the prosody baseline")
