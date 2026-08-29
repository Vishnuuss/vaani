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

import re
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


# Punctuation and case are STT artefacts, not things the caller said
# differently. Comparing them makes an identical utterance look like a miss.
_PUNCT = re.compile(r"[.,!?;:।‘’“”]+")


def _normalise(text: str) -> list[str]:
    return _PUNCT.sub("", (text or "").lower()).split()


def _is_prefix(prefix: list[str], words: list[str]) -> bool:
    return len(prefix) <= len(words) and words[: len(prefix)] == prefix


# At most this many generations per caller turn.
#
# Speculation was disabled once before after run 92 lost a call entirely -- zero
# pipeline events. The probe was a pass-through on frames but issued REAL
# generations, and with hedging already sending three requests per turn, firing
# again on every partial multiplies concurrent load on the provider. The
# suspected failure is contention, not logic.
#
# Two is enough to win the case: the last partial before the caller stops is
# usually the whole utterance, and the one before it catches the case where the
# final arrives while a generation is still in flight. Beyond that each extra
# firing buys less and costs more.
MAX_SPECULATIONS_PER_TURN = 2

# A one-word partial is skipped. It is the least likely to be the final text and
# the most likely to be someone mid-sentence -- run 287's "మాది." was exactly
# that, and answering it would have been the interruption this must never cause.
MIN_WORDS_TO_SPECULATE = 2


class Speculator:
    """Decides when to speculate, when to abandon it, and scores the result."""

    def __init__(self) -> None:
        self._tracker = StablePrefixTracker()
        self._speculated: list[str] | None = None
        self._stats = SpeculationStats()
        self._fired_this_turn = 0

    def on_partial(self, partial: str) -> SpecCommand:
        words = partial.split()

        # Abandon an in-flight speculation the moment the decoder contradicts
        # the words it was built on.
        if self._speculated is not None and not _is_prefix(self._speculated, words):
            self._speculated = None
            self._stats.cancels += 1
            return SpecCommand(action=SpecAction.CANCEL)

        result = self._tracker.observe(partial)
        if (result.action is Action.FIRE
                and len(result.stable_prefix.split()) < MIN_WORDS_TO_SPECULATE):
            return SpecCommand(action=SpecAction.HOLD)
        if self._fired_this_turn >= MAX_SPECULATIONS_PER_TURN:
            return SpecCommand(action=SpecAction.HOLD)
        if result.action is Action.FIRE and result.stable_prefix:
            self._fired_this_turn += 1
            self._speculated = result.stable_prefix.split()
            self._stats.fired += 1
            return SpecCommand(action=SpecAction.FIRE, text=result.stable_prefix)

        return SpecCommand(action=SpecAction.HOLD)

    def on_turn_end(self, final_text: str) -> Outcome:
        self._stats.turns += 1
        speculated, self._speculated = self._speculated, None

        if speculated is None:
            return Outcome.NO_SPECULATION

        final_words = _normalise(final_text)
        speculated = _normalise(" ".join(speculated))
        # A HIT is "the speculated request would have produced the right reply",
        # not "the two strings are byte-identical". The old test was
        # `speculated == final_words` on raw splits, and it could essentially
        # never be true: what we speculate on is the common prefix of two
        # partials, which is SHORTER than the final by construction. The 0% hit
        # rate that switched speculation off was measuring an impossible
        # condition, not measuring speculation.
        if speculated == final_words:
            self._stats.hits += 1
            return Outcome.HIT
        if _is_prefix(speculated, final_words):
            # The caller only added words we would have heard anyway. The
            # generated reply is still the reply to their question.
            self._stats.partials += 1
            return Outcome.PARTIAL

        self._stats.misses += 1
        return Outcome.MISS

    def reset_turn(self) -> None:
        self._tracker = StablePrefixTracker()
        self._speculated = None
        self._fired_this_turn = 0

    @property
    def stats(self) -> SpeculationStats:
        return self._stats
