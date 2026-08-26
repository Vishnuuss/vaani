"""Stable-prefix tracking over non-monotonic STT partials."""

from dataclasses import dataclass
from enum import Enum


class Action(Enum):
    HOLD = "hold"
    FIRE = "fire"
    CANCEL = "cancel"


@dataclass
class PrefixResult:
    stable_prefix: str
    action: Action


def _common_word_prefix(a: list[str], b: list[str]) -> list[str]:
    out: list[str] = []
    for left, right in zip(a, b):
        if left != right:
            break
        out.append(left)
    return out


class StablePrefixTracker:
    """A word is 'stable' once two consecutive partials agree on it."""

    def __init__(self) -> None:
        self._previous: list[str] = []
        self._fired_word_count = 0

    def observe(self, partial: str) -> PrefixResult:
        words = partial.split()
        stable = _common_word_prefix(self._previous, words)
        self._previous = words

        # Only a prefix that has GROWN past what we already speculated on is
        # worth a new call. A shrinking partial means the decoder is revising
        # backwards, so we hold rather than fire on words that may vanish.
        if len(stable) > self._fired_word_count:
            self._fired_word_count = len(stable)
            action = Action.FIRE
        else:
            action = Action.HOLD

        return PrefixResult(stable_prefix=" ".join(stable), action=action)
