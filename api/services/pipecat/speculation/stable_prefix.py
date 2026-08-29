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
    """Chooses the text a speculative generation should run on.

    Why this is the WHOLE partial and not a two-partial stable prefix
    -----------------------------------------------------------------
    The original rule fired on the common prefix of the last two partials, and
    that rule can never contain the newest word. On a short Telugu answer it is
    therefore guaranteed to miss:

        partial "వన్"              fires on nothing
        partial "వన్ లాక్"          fires on "వన్"
        partial "వన్ లాక్ అండి"     fires on "వన్ లాక్"
        final   "వన్ లాక్ అండి"     MISS -- one word behind, always

    That is exactly what the live probe measured: a 0% hit rate over nine turns.
    The conclusion drawn at the time was that speculation does not work on this
    traffic. The real fault was this rule. These callers answer in two or three
    words, so a trigger that structurally excludes the last word can never fire
    on a complete answer.

    So the newest partial IS the candidate. It is what the caller has said so
    far, and when they stop it is usually the whole utterance. A generation
    started on it is either still valid at turn end -- a hit -- or contradicted,
    in which case it is cancelled and nothing was ever spoken.

    Being wrong is cheap and being right is worth ~0.28s: a cancelled
    speculation costs tokens on a prompt that is ~85% provider-cached, and it
    can never reach the caller, because the coordinator only hands tokens over
    when the turn has genuinely ended and the text matches exactly.
    """

    def __init__(self) -> None:
        self._previous: list[str] = []
        self._fired_word_count = 0

    def observe(self, partial: str) -> PrefixResult:
        words = partial.split()
        self._previous = words

        # Fire whenever the caller has said MORE than the words already
        # speculated on. A shrinking partial means the decoder is revising
        # backwards; hold, and let the contradiction check cancel the in-flight
        # generation rather than starting another on unstable text.
        if len(words) > self._fired_word_count and words:
            self._fired_word_count = len(words)
            return PrefixResult(stable_prefix=" ".join(words), action=Action.FIRE)

        return PrefixResult(stable_prefix=" ".join(words), action=Action.HOLD)
