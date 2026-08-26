"""Speculative-turn accounting.

Wraps :class:`StablePrefixTracker` and records what actually happened, so the
speculation hit rate — the number the <700 ms budget rests on — is measured
rather than assumed.

The accounting deliberately separates three outcomes that are easy to blur:

``HIT``      the caller's final text is exactly what we speculated on. The
             pre-generated response is usable as-is and the LLM leaves the
             critical path entirely.
``PARTIAL``  we speculated on a true prefix, but the caller kept talking. The
             response was built without the rest of the sentence, so it is NOT
             a clean win and must never be counted as one.
``MISS``     the decoder revised and we speculated on words that were never
             said. The work is thrown away and the turn pays full latency.
"""

from dataclasses import dataclass
from enum import Enum

from api.services.pipecat.speculation.stable_prefix import (
    Action,
    StablePrefixTracker,
)


class SpecAction(Enum):
    HOLD = "hold"
    FIRE = "fire"
    CANCEL = "cancel"


class Outcome(Enum):
    HIT = "hit"
    PARTIAL = "partial"
    MISS = "miss"
    NO_SPECULATION = "no_speculation"


@dataclass
class SpecCommand:
    action: SpecAction
    text: str = ""


@dataclass
class SpeculationStats:
    turns: int = 0
    fired: int = 0
    hits: int = 0
    partials: int = 0
    misses: int = 0
    cancels: int = 0

    @property
    def hit_rate(self) -> float:
        """Clean hits over all turns.

        PARTIAL is excluded on purpose. Counting it would inflate the very
        number the latency case depends on.
        """
        if self.turns == 0:
            return 0.0
        return self.hits / self.turns


def _is_prefix(prefix: list[str], words: list[str]) -> bool:
    return len(prefix) <= len(words) and words[: len(prefix)] == prefix


class Speculator:
    """Decides when to speculate, when to abandon it, and scores the result."""

    def __init__(self) -> None:
        self._tracker = StablePrefixTracker()
        self._speculated: list[str] | None = None
        self._stats = SpeculationStats()

    def on_partial(self, partial: str) -> SpecCommand:
        words = partial.split()

        # Abandon an in-flight speculation the moment the decoder contradicts
        # the words it was built on.
        if self._speculated is not None and not _is_prefix(self._speculated, words):
            self._speculated = None
            self._stats.cancels += 1
            return SpecCommand(action=SpecAction.CANCEL)

        result = self._tracker.observe(partial)
        if result.action is Action.FIRE and result.stable_prefix:
            self._speculated = result.stable_prefix.split()
            self._stats.fired += 1
            return SpecCommand(action=SpecAction.FIRE, text=result.stable_prefix)

        return SpecCommand(action=SpecAction.HOLD)

    def on_turn_end(self, final_text: str) -> Outcome:
        self._stats.turns += 1
        speculated, self._speculated = self._speculated, None

        if speculated is None:
            return Outcome.NO_SPECULATION

        final_words = final_text.split()
        if speculated == final_words:
            self._stats.hits += 1
            return Outcome.HIT
        if _is_prefix(speculated, final_words):
            self._stats.partials += 1
            return Outcome.PARTIAL

        self._stats.misses += 1
        return Outcome.MISS

    def reset_turn(self) -> None:
        self._tracker = StablePrefixTracker()
        self._speculated = None

    @property
    def stats(self) -> SpeculationStats:
        return self._stats
