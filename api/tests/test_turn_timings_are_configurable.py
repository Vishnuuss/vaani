"""Every turn-detector timing must be reachable from workflow_configurations.

Why this test exists
--------------------
`create_user_turn_stop_strategies` forwarded four values out of fourteen. The
other ten lived as `TeluguTurnParams` defaults or as module constants in
`telugu_turn.py`, so changing any of them -- including the 450 ms floor applied
to a short utterance whose transcript has not arrived -- required editing Python
and redeploying the voice container.

That is the defect the client named on 7 Sep: *"hardcoding the turn detection to
450ms without understanding intent is not good"*. How long to wait before
deciding a person has stopped speaking is a per-agent, per-language judgement,
and it cannot be a judgement while it needs a deploy.

The failure mode this guards against is silent. A forgotten key does not raise;
the analyzer simply keeps its default, the setting appears in the config and
does nothing, and it looks identical to a working one until somebody measures a
call. So the test asserts the whole surface at once and will fail the moment a
new timing is added to `TeluguTurnParams` without a route from config.
"""

from __future__ import annotations

import pytest

from api.services.vaani.turn_taking import create_user_turn_stop_strategies, analyzer_from


# key in workflow_configurations -> attribute on TeluguTurnParams
ROUTES = {
    "smart_turn_stop_secs": ("stop_secs", 0.55),
    "endpoint_min_secs": ("min_endpoint_secs", 0.11),
    "endpoint_max_secs": ("max_endpoint_secs", 2.75),
    "endpoint_fragment_floor_secs": ("fragment_floor_secs", 1.23),
    "endpoint_unsure_floor_secs": ("unsure_floor_secs", 0.44),
    "endpoint_unsure_band": ("unsure_band", 0.77),
    "blind_min_silence_ms": ("blind_min_silence_ms", 321.0),
    "blind_short_silence_ms": ("blind_short_silence_ms", 654.0),
    "turn_fragment_secs": ("fragment_secs", 0.87),
    "turn_short_threshold": ("short_threshold", 0.91),
    "turn_min_silence_ms": ("min_silence_ms", 199.0),
    "turn_window_secs": ("window_secs", 2.25),
    "turn_resume_window_secs": ("resume_window_secs", 1.75),
    "turn_cutoffs_before_adapting": ("cutoffs_before_adapting", 5),
}


def _params(run_configs: dict):
    strategies = create_user_turn_stop_strategies(
        run_configs, uses_external_turns=False)
    analyzer = analyzer_from(strategies)
    assert analyzer is not None, "no turn analyzer was built"
    return analyzer.params


@pytest.mark.parametrize("key,route", sorted(ROUTES.items()))
def test_each_timing_reaches_the_analyzer(key, route):
    attr, value = route
    params = _params({"turn_stop_strategy": "turn_analyzer", key: value})
    got = getattr(params, attr)
    assert got == pytest.approx(value), (
        f"workflow_configurations['{key}'] did not reach "
        f"TeluguTurnParams.{attr}: got {got!r}, expected {value!r}. "
        "A timing that cannot be set from config needs a code deploy to change."
    )


def test_all_of_them_at_once():
    """Together, not just one at a time -- a later key must not clobber an earlier."""
    cfg = {"turn_stop_strategy": "turn_analyzer"}
    cfg.update({k: v for k, (_, v) in ROUTES.items()})
    params = _params(cfg)
    for key, (attr, value) in ROUTES.items():
        assert getattr(params, attr) == pytest.approx(value), f"{key} was lost"


def test_empty_config_keeps_todays_behaviour():
    """An agent with `workflow_configurations == {}` must be unchanged.

    Every agent built in the new editor starts with an empty config, and this is
    where the previous defect of this shape landed: a bare `.get()` returned
    None, the declared default was never reached, and every call silently fell
    through to a fixed 0.6 s timeout. See test_turn_stop_default_is_applied.
    """
    from api.schemas.workflow_configurations import (
        DEFAULT_BLIND_SHORT_SILENCE_MS,
        DEFAULT_ENDPOINT_MAX_SECS,
        DEFAULT_ENDPOINT_MIN_SECS,
        DEFAULT_ENDPOINT_UNSURE_FLOOR_SECS,
        DEFAULT_TURN_FRAGMENT_SECS,
    )

    params = _params({})
    assert params.min_endpoint_secs == pytest.approx(DEFAULT_ENDPOINT_MIN_SECS)
    assert params.max_endpoint_secs == pytest.approx(DEFAULT_ENDPOINT_MAX_SECS)
    assert params.unsure_floor_secs == pytest.approx(DEFAULT_ENDPOINT_UNSURE_FLOOR_SECS)
    assert params.blind_short_silence_ms == pytest.approx(DEFAULT_BLIND_SHORT_SILENCE_MS)
    assert params.fragment_secs == pytest.approx(DEFAULT_TURN_FRAGMENT_SECS)


def test_no_timing_is_left_unreachable():
    """The whole point: every tunable on TeluguTurnParams has a config route.

    Deliberately not a hand-written list of exceptions. `threshold` comes from
    the trained model, `max_duration_secs` bounds a buffer rather than a
    decision, and the four `*_endpoint_secs`/`stop_secs` names are already
    covered above under their config spellings.
    """
    from api.services.vaani.telugu_turn import TeluguTurnParams

    routed = {attr for attr, _ in ROUTES.values()}
    exempt = {"threshold", "max_duration_secs"}
    tunables = {
        name for name in TeluguTurnParams.__dataclass_fields__
        if not name.startswith("_")
    } if hasattr(TeluguTurnParams, "__dataclass_fields__") else {
        name for name in TeluguTurnParams.model_fields
    }

    unreachable = tunables - routed - exempt
    assert not unreachable, (
        f"these turn timings cannot be set from workflow_configurations: "
        f"{sorted(unreachable)}. Add a DEFAULT_* and a route in "
        f"turn_taking.create_user_turn_stop_strategies, or add to `exempt` with "
        f"a reason."
    )
