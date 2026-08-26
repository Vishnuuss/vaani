"""Overlaps the turn boundary — the architectural change, not a tuning knob.

Dograh's pipeline is reactive: it waits for the caller to stop, then runs
STT -> LLM -> TTS in sequence. This coordinator starts the LLM **while the
caller is still speaking**, on the longest stable prefix of their speech, and
throws the work away if the decoder later contradicts it.

    caller speaking ──▶ stable prefix ──▶ generation starts (in background)
    caller stops    ──▶ text matches?  ──▶ replay buffered tokens, LLM ≈ 0 ms
                    ──▶ text differs?  ──▶ cancel, normal path runs

Only an exact match replays. A prefix match (the caller kept talking) is
deliberately treated as a miss: answering half a sentence is worse than being
250 ms slower.

`generate` is injected — an async callable taking the speculated user text and
yielding token strings — so this stays provider-agnostic and testable offline.
"""

import asyncio
from typing import AsyncIterator, Callable, Optional

from loguru import logger

from api.services.pipecat.speculation.speculator import (
    Outcome,
    SpecAction,
    SpeculationStats,
    Speculator,
)

GenerateFn = Callable[[str], AsyncIterator[str]]


class SpeculationCoordinator:
    """Runs, cancels and scores speculative generations across a turn."""

    def __init__(self, generate: GenerateFn):
        self._generate = generate
        self._speculator = Speculator()
        self._task: Optional[asyncio.Task] = None
        self._speculated_text: Optional[str] = None
        # Set by SpeculationProbe: the aggregator eats TranscriptionFrame
        # before the gate can see it, so the gate cannot learn the final
        # text on its own.
        self.pending_final_text: str = ""

    async def on_partial(self, text: str) -> None:
        """Feed one interim transcript in; may start or abandon a generation."""
        command = self._speculator.on_partial(text)

        if command.action is SpecAction.FIRE:
            await self._cancel_inflight()
            self._speculated_text = command.text
            self._task = asyncio.create_task(
                self._collect(command.text), name="speculative-generation"
            )
        elif command.action is SpecAction.CANCEL:
            await self._cancel_inflight()
            self._speculated_text = None

    async def take_response_for(self, final_text: str) -> Optional[list[str]]:
        """Claim the pre-generated response for this turn, if it is usable.

        Returns the tokens on an exact hit — the caller should then skip the
        real LLM entirely — or None, in which case the normal path must run.
        """
        outcome = self._speculator.on_turn_end(final_text)

        if outcome is not Outcome.HIT:
            await self._cancel_inflight()
            self._speculated_text = None
            return None

        task, self._task = self._task, None
        self._speculated_text = None
        if task is None:
            return None

        try:
            # Already in flight since mid-turn, so this resolves far sooner
            # than a generation started from scratch here.
            tokens = await task
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.warning(f"[speculation] generation failed, falling back: {e}")
            return None

        logger.info(f"[speculation] HIT — replaying {len(tokens)} pre-generated tokens")
        return tokens

    def reset_turn(self) -> None:
        self._speculator.reset_turn()

    async def cleanup(self) -> None:
        await self._cancel_inflight()

    @property
    def stats(self) -> SpeculationStats:
        return self._speculator.stats

    async def _collect(self, text: str) -> list[str]:
        tokens: list[str] = []
        async for token in self._generate(text):
            tokens.append(token)
        return tokens

    async def _cancel_inflight(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
