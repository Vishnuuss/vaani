"""Speculative execution -- start thinking before the caller finishes talking.

Without this, sub-600ms is not reachable on this stack. Measured first-token
latency is ~330-460ms; the only way to stop paying it is to have the request
already in flight when the caller stops speaking, so it happens UNDER their own
speech and costs nothing.

    on each stabilised partial:  cancel in-flight, re-fire, buffer tokens
    on turn commit:              HIT  -> release the buffer straight to TTS
                                 MISS -> cancel, fire for real, cover the gap

CRITICAL, learned from live Sarvam data (FINDINGS §5b): partials are NOT
monotonic. They revise backwards:

    "నా ఇల్లు నా"
    "నా ఇల్లు నా ఇల్లు"
    "నా ఇల్లు"              <- SHORTER than the previous partial
    "నా ఇల్లు నా ఇల్లు నాదే"

A naive "re-fire whenever the text changed" rule would speculate on text the
caller never said. So we track the longest STABLE prefix and treat a shrinking
partial as a signal to hold, not to fire.

Two safety rules, both absolute:
  * A speculative turn NEVER causes a side effect -- no tool calls, no CRM
    writes, no state mutation. It produces candidate text and nothing else.
  * At most MAX_INFLIGHT speculations at once. Beyond that we buy cost without
    buying latency.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

MAX_INFLIGHT = 2

# Re-fire only once the stable prefix gains this much new material. Firing on
# every token burns tokens for no latency gain, because the request would be
# cancelled before it produced anything useful.
MIN_NEW_CHARS = 6


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", (text or "").strip()) if t]


def stable_prefix(a: str, b: str) -> str:
    """Longest common leading token run of two partials.

    Token-wise, not character-wise: a character-wise prefix would treat
    "మూడు" -> "మూడువేలు" as stable through a word boundary and let us
    speculate on half a word.
    """
    ta, tb = _tokens(a), _tokens(b)
    out = []
    for x, y in zip(ta, tb):
        if x != y:
            break
        out.append(x)
    return " ".join(out)


@dataclass
class Speculation:
    text: str                       # the transcript this was fired on
    task: asyncio.Task
    buffer: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.perf_counter)
    complete: bool = False
    error: BaseException | None = None

    @property
    def output(self) -> str:
        return "".join(self.buffer)

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


class Speculator:
    """Owns in-flight speculative generations for one call."""

    def __init__(self, llm, build_messages, *, max_inflight: int = MAX_INFLIGHT):
        self.llm = llm
        self.build_messages = build_messages     # (transcript) -> messages list
        self.max_inflight = max_inflight
        self._inflight: list[Speculation] = []
        self._last_partial = ""
        self._last_fired_on = ""
        self.hits = 0
        self.misses = 0

    # -- statistics the plan judges us on ---------------------------------
    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    # -- driving ----------------------------------------------------------
    def on_partial(self, partial: str) -> bool:
        """Feed a new partial transcript. Returns True if a speculation fired."""
        prev, self._last_partial = self._last_partial, partial

        # Shrinking or revising partial -> the decoder is unsure. Hold.
        if len(_tokens(partial)) < len(_tokens(prev)):
            return False

        stable = stable_prefix(prev, partial) if prev else partial
        if not stable:
            return False

        # Only re-fire once the stable prefix has genuinely advanced.
        if len(stable) < len(self._last_fired_on) + MIN_NEW_CHARS:
            return False

        self._fire(stable)
        return True

    def _fire(self, transcript: str) -> None:
        # Keep only the newest speculations; older ones cannot win any more.
        while len(self._inflight) >= self.max_inflight:
            self._inflight.pop(0).cancel()

        spec = Speculation(text=transcript, task=None)  # type: ignore[arg-type]
        spec.task = asyncio.create_task(self._run(spec))
        self._inflight.append(spec)
        self._last_fired_on = transcript

    async def _run(self, spec: Speculation) -> None:
        try:
            messages = self.build_messages(spec.text)
            async for delta in self.llm.stream(messages):
                spec.buffer.append(delta)
            spec.complete = True
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - recorded, not raised here
            spec.error = exc

    # -- committing -------------------------------------------------------
    def commit(self, final_transcript: str) -> Speculation | None:
        """Called when the endpointer says the turn is over.

        Returns a usable Speculation on a HIT, or None on a MISS (caller must
        fire a real request and cover the gap).
        """
        match = None
        for spec in self._inflight:
            if spec.error is not None:
                continue
            # A hit means we speculated on what the caller actually said. We
            # accept the speculation only if its transcript is a prefix of the
            # final one AND close enough that the answer would not change.
            if final_transcript.startswith(spec.text) and \
                    len(final_transcript) - len(spec.text) <= MIN_NEW_CHARS:
                match = spec

        for spec in self._inflight:
            if spec is not match:
                spec.cancel()
        self._inflight = [match] if match else []

        if match is not None:
            self.hits += 1
        else:
            self.misses += 1
            self._last_fired_on = ""
        return match

    def cancel_all(self) -> None:
        for spec in self._inflight:
            spec.cancel()
        self._inflight.clear()
        self._last_fired_on = ""
