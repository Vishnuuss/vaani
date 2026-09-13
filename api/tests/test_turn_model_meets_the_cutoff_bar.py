"""The shipped turn model may never cut callers off more than 2% of the time.

`latency_budget.yaml` sets `max_false_interruption_rate: 0.02`. Until 13 Sep
the model on disk did not meet it and nobody could tell, because it was trained
on `turnstops.jsonl` -- 443 rows in this checkout, **every one**
`was_turn_end: True`. With no negatives there was nothing to score a cut-off
rate against, so the artifact carried no such number at all.

Scored on the real-label holdout (`tools/score_turn_models.py`, by-call split,
`turnstops_real.jsonl`: 1,707 completions and 1,243 real negatives from 647
calls):

    SHIPPED    threshold 0.97   false cutoffs 6.5%   endable early 12.4%   FAILS
    retrained forest  0.83                    1.5%                 10.0%   ok
    retrained linear  0.72                    1.2%                 10.7%   ok

Both retrained artifacts are now on disk, so whichever `turn_model` selects, a
caller is not cut off 6.5% of the time.

This test does not re-score the model -- that needs the audio corpus, which is
not in the repo. It asserts the artifact CARRIES its measured rate and that the
rate clears the bar. An artifact trained without negatives cannot satisfy this,
which is exactly the hole being closed: the failure mode was a model with no
number, not a model with a bad one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MODELS = Path(__file__).resolve().parents[1] / "services" / "vaani" / "models"

# latency_budget.yaml: max_false_interruption_rate
MAX_FALSE_CUTOFF = 0.02

ARTIFACTS = ["audio_turn_weights.json", "audio_turn_gbm.json"]


@pytest.mark.parametrize("name", ARTIFACTS)
def test_the_artifact_records_its_own_cutoff_rate(name):
    """A model with no measured cut-off rate must not be shipped."""
    blob = json.loads((MODELS / name).read_text(encoding="utf-8"))
    assert "false_cutoff_rate" in blob, (
        f"{name} carries no false_cutoff_rate -- it cannot have been scored "
        "against negatives, which is how the 6.5% model shipped unnoticed")
    assert "early_end_rate" in blob


@pytest.mark.parametrize("name", ARTIFACTS)
def test_the_cutoff_rate_clears_the_budget(name):
    blob = json.loads((MODELS / name).read_text(encoding="utf-8"))
    rate = float(blob["false_cutoff_rate"])
    assert rate <= MAX_FALSE_CUTOFF, (
        f"{name} cuts callers off {rate * 100:.1f}% of the time, over the "
        f"{MAX_FALSE_CUTOFF * 100:.0f}% budget")


@pytest.mark.parametrize("name", ARTIFACTS)
def test_it_was_trained_against_real_negatives(name):
    """The specific defect: a training set with no incomplete turns in it."""
    blob = json.loads((MODELS / name).read_text(encoding="utf-8"))
    assert blob.get("source_dataset") == "turnstops_real.jsonl"
    assert int(blob.get("n_incomplete") or 0) > 0, (
        f"{name} was trained with no negative examples")


@pytest.mark.parametrize("name", ARTIFACTS)
def test_it_still_ends_a_useful_share_of_turns_early(name):
    """A model that never fires clears the cut-off bar and is worthless.

    Smart Turn v3 is the worked example: on Telugu it measured p50 == p90 ==
    2.18s, which is a 2.2s stopwatch wearing a model's clothes.
    """
    blob = json.loads((MODELS / name).read_text(encoding="utf-8"))
    assert float(blob["early_end_rate"]) >= 0.05, (
        f"{name} ends {float(blob['early_end_rate']) * 100:.1f}% of turns "
        "early -- it is a timer, not a detector")


@pytest.mark.parametrize("name", ARTIFACTS)
def test_the_threshold_is_the_one_that_was_scored(name):
    """The rates above are only true AT the threshold they were measured at."""
    blob = json.loads((MODELS / name).read_text(encoding="utf-8"))
    assert 0.0 < float(blob["threshold"]) < 1.0
    assert int(blob.get("n_features") or 0) == 16


def test_the_loader_accepts_the_retrained_linear_artifact():
    """Schema check: the retrained file drops `features`, which the deployed
    one had. `_load` reads mean/scale/coef/intercept/threshold only, but this
    pins it so a future loader change cannot break the swap silently."""
    blob = json.loads((MODELS / "audio_turn_weights.json").read_text(encoding="utf-8"))
    for key in ("mean", "scale", "coef"):
        assert len(blob[key]) == 16
    assert isinstance(float(blob["intercept"]), float)
