"""Which turn model runs must be a CONFIG choice, not a file on disk.

Until 7 Sep `TeluguTurnAnalyzer.__init__` did:

    self.enabled = (self._load_forest(...) or self._load(...))

so a forest on disk always won and the logistic weights beside it were
unreachable. Shipping a better linear model meant DELETING the forest file --
a change that cannot be reverted from config, on a live client agent.

The third mode, "timer", exists because of a measurement. On 60 recorded calls
(235 speech bursts) a model-free wait beat every trained model at equal
patience: a 0.6s flat timer cut 19.1% of turns off at a 0.80s median wait,
while the retrained linear model cut off the same 19.1% and took 0.98s. "No
verdict" has to be expressible before it can be tested, so it is a value here
rather than a code edit.
"""

from __future__ import annotations

from api.schemas.workflow_configurations import (
    DEFAULT_TURN_MODEL,
    WorkflowConfigurationDefaults,
)
from api.services.vaani.telugu_turn import TeluguTurnAnalyzer

SR = 8000


def test_the_default_is_still_the_forest():
    """Nothing changes until somebody changes it deliberately."""
    assert DEFAULT_TURN_MODEL == "forest"
    assert WorkflowConfigurationDefaults().turn_model == "forest"


def test_forest_and_linear_both_load_and_are_different_models():
    forest = TeluguTurnAnalyzer(sample_rate=SR, model_kind="forest")
    linear = TeluguTurnAnalyzer(sample_rate=SR, model_kind="linear")
    assert forest.enabled and linear.enabled
    # The forest is a tree ensemble; the linear model is coefficients. If
    # selection silently fell through, both would load the same one.
    assert forest._forest is not None
    assert linear._coef is not None


def test_timer_mode_loads_no_model_and_never_fires():
    """The floors decide alone: the threshold must be unreachable."""
    a = TeluguTurnAnalyzer(sample_rate=SR, model_kind="timer")
    assert a.enabled is False
    assert a.params.threshold > 1.0


def test_an_unknown_value_falls_back_to_today_rather_than_breaking_a_call():
    """A typo in stored config must not silence an agent."""
    a = TeluguTurnAnalyzer(sample_rate=SR, model_kind="nonsense")
    assert a.enabled is True
    assert a._forest is not None


def test_timer_is_a_legal_config_value():
    assert WorkflowConfigurationDefaults(turn_model="timer").turn_model == "timer"
    assert WorkflowConfigurationDefaults(turn_model="linear").turn_model == "linear"
