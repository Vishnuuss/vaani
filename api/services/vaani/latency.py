"""Vaani enforces the latency budget.

`latency_budget.yaml` opens with:

    THIS FILE IS THE SINGLE SOURCE OF TRUTH FOR THE 800 ms TARGET.
    It is not documentation. It is loaded at runtime, enforced on every turn,
    and validated in CI.

Until this module existed that claim was false on this side -- the file was not
loaded anywhere in the repo. This makes it true.

How it enforces, without instrumenting the pipeline
---------------------------------------------------
Pipecat already computes a full per-turn `LatencyBreakdown` when
`enable_metrics=True`. Vaani consumes that rather than wrapping every stage in
its own span, so enforcement costs nothing on the critical path and cannot
itself become a source of latency.

One honest mapping caveat: the budget file splits `endpoint_detection` (250 ms)
from `stt_finalize` (100 ms), but pipecat reports a single `user_turn_secs` that
contains BOTH (VAD silence + STT finalisation + turn-analyzer wait). They are
therefore checked against their combined allocation, and the report says so
rather than pretending to a split it cannot see.

`transport` (50 ms) is not observable from inside the process and is skipped.

Enforcement never breaks a live call. The yaml sets `prod: warn` deliberately.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

BUDGET_FILE = Path(__file__).resolve().parent / "latency_budget.yaml"

# Which budget components each observable maps to.
_USER_TURN_COMPONENTS = ("endpoint_detection", "stt_finalize")
_LLM_COMPONENT = "llm_first_spoken_token"
_TTS_COMPONENT = "tts_first_audio"


class BudgetInvalid(ValueError):
    """The budget file itself does not add up."""


class BudgetExceeded(RuntimeError):
    """A turn overran its allocation, in an environment set to strict."""


@dataclass(frozen=True)
class Violation:
    scope: str
    actual_ms: float
    budget_ms: float

    @property
    def over_ms(self) -> float:
        return self.actual_ms - self.budget_ms

    def __str__(self) -> str:
        pct = (self.over_ms / self.budget_ms * 100) if self.budget_ms else 0.0
        return (
            f"{self.scope}: {self.actual_ms:.0f}ms over budget {self.budget_ms:.0f}ms "
            f"(+{self.over_ms:.0f}ms, {pct:.0f}%)"
        )


@dataclass(frozen=True)
class Budget:
    target_p50_ms: float
    target_p95_ms: float
    components: dict[str, float]

    def allocation(self, *names: str) -> float:
        return sum(self.components[n] for n in names)


@lru_cache(maxsize=1)
def load_budget(path: str | None = None) -> Budget:
    raw = yaml.safe_load(Path(path or BUDGET_FILE).read_text(encoding="utf-8"))
    components = {
        name: float(spec["budget_ms"]) for name, spec in raw["components"].items()
    }
    return Budget(
        target_p50_ms=float(raw["target"]["p50_ms"]),
        target_p95_ms=float(raw["target"]["p95_ms"]),
        components=components,
    )


def validate_budget(budget: Budget | None = None) -> None:
    """The rule the yaml states: components MUST NOT sum past the target.

    You cannot give one component more time without taking it from another.
    Called from a test, so the build fails if someone quietly raises an
    allocation.
    """
    budget = budget or load_budget()
    total = sum(budget.components.values())
    if total > budget.target_p50_ms:
        raise BudgetInvalid(
            f"component budgets sum to {total:.0f}ms, over the "
            f"{budget.target_p50_ms:.0f}ms target by {total - budget.target_p50_ms:.0f}ms"
        )


def enforcement_mode() -> str:
    """`strict` raises, anything else warns. Never strict by default."""
    return os.environ.get("VAANI_LATENCY_ENFORCEMENT", "warn").lower()


def check_breakdown(breakdown, *, budget: Budget | None = None) -> list[Violation]:
    """Score one pipecat ``LatencyBreakdown`` against the budget."""
    budget = budget or load_budget()
    violations: list[Violation] = []

    user_turn = getattr(breakdown, "user_turn_secs", None)
    if user_turn is not None:
        allowed = budget.allocation(*_USER_TURN_COMPONENTS)
        actual = user_turn * 1000
        if actual > allowed:
            violations.append(
                Violation("endpoint+stt_finalize (user_turn_secs)", actual, allowed)
            )

    for metric in getattr(breakdown, "ttfb", []) or []:
        processor = (getattr(metric, "processor", "") or "").lower()
        actual = float(getattr(metric, "duration_secs", 0.0)) * 1000
        if "tts" in processor:
            component = _TTS_COMPONENT
        elif "llm" in processor:
            component = _LLM_COMPONENT
        else:
            continue
        allowed = budget.components[component]
        if actual > allowed:
            violations.append(
                Violation(f"{component} ({getattr(metric, 'processor', '?')})",
                          actual, allowed)
            )

    return violations


def report(breakdown, *, turn: int | None = None, budget: Budget | None = None) -> list[Violation]:
    """Check a turn and log the result. Raises only under strict enforcement."""
    try:
        violations = check_breakdown(breakdown, budget=budget)
    except Exception as e:  # diagnostics must never break a live call
        logger.debug(f"[budget] check failed, ignored: {e}")
        return []

    if not violations:
        return []

    where = f"turn {turn}" if turn is not None else "turn"
    detail = " | ".join(str(v) for v in violations)
    if enforcement_mode() == "strict":
        raise BudgetExceeded(f"{where} broke the latency budget: {detail}")
    logger.warning(f"[budget] {where} OVER: {detail}")
    return violations
