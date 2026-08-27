"""Fire the same completion twice and speak whichever answers first.

The measurement
---------------
`bench/hedge.py`, 2026-08-27, identical prompt and params against Groq
`openai/gpt-oss-120b` at reasoning_effort=low, 24 single draws:

    single    p50 0.517   p90 1.132   max 1.450

The p50 was never the problem. The spread was: the same request, byte for byte,
returned its first speakable token anywhere between 0.289s and 1.450s. A caller
does not experience a median. They experience the turn they are in, and roughly
one turn in ten was landing past a second on the LLM alone.

Ten rounds of two concurrent requests, taking whichever produced content first:

    hedge-2   p50 0.355   p90 0.390   max 0.390

That is 0.742s off the p90, and the distribution collapses -- the worst hedged
turn measured was faster than the median un-hedged one.

Why it beats its own simulation
-------------------------------
Drawing two samples from the measured marginal distribution and taking the min
predicts p90 0.734s. Reality gave 0.390s. So the two requests are not two draws
from one distribution: the slow tail is a busy-worker effect, and a second
request lands on a different worker, so it reliably dodges whatever the first
one got stuck behind. This is Dean and Barroso's "The Tail at Scale" -- hedging
works precisely when the tail comes from transient server-side contention
rather than from the work itself being hard.

Why race on the first CONTENT token, not the first chunk
--------------------------------------------------------
gpt-oss is a reasoning model: its first chunks carry the `analysis` channel,
which the caller cannot hear. The first chunk arrives at ~0.2s while the first
content arrives at ~0.5s, and the two are not correlated -- the request that
opens its stream soonest is not the one that finishes thinking soonest. Racing
on the first chunk would pick the wrong winner and give back most of the gain.

Cost
----
The loser is cancelled the moment the winner produces content, so it bills a few
dozen reasoning tokens and nothing else. Input is ~96% cached. The measured turn
cost rises well under 2x. Set `hedge=1` to switch the whole thing off.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.settings import assert_given

DEFAULT_HEDGE = 2

# A hedge only pays while the tail is server-side contention. Past a handful of
# copies we would be adding load and creating the contention ourselves.
MAX_HEDGE = 3


def _has_content(chunk: Any) -> bool:
    """True once a chunk carries something that ends the turn."""
    try:
        for choice in chunk.choices or []:
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "content", None):
                return True
            # A tool call is a real answer too -- it completes the turn just as
            # a spoken reply does, so it must be allowed to win the race.
            if getattr(delta, "tool_calls", None):
                return True
    except Exception:
        return False
    return False


class HedgedGroqLLMService(GroqLLMService):
    """GroqLLMService that races N identical completions and keeps the winner."""

    def __init__(self, *, hedge: int = DEFAULT_HEDGE, **kwargs):
        super().__init__(**kwargs)
        self._hedge = max(1, min(int(hedge), MAX_HEDGE))
        if self._hedge > 1:
            logger.info(f"[hedge] racing {self._hedge} completions per turn")

    async def get_chat_completions(self, context: LLMContext):
        """Return the chunk stream of whichever copy speaks first."""
        if self._hedge <= 1:
            return await super().get_chat_completions(context)

        # Build the request exactly as the parent would, once, so every copy is
        # byte-identical and they all share the same cached prefix.
        adapter = self.get_llm_adapter()
        params_from_context = adapter.get_llm_invocation_params(
            context,
            system_instruction=assert_given(self._settings.system_instruction),
            convert_developer_to_user=not self.supports_developer_role,
        )
        params = self.build_chat_completion_params(params_from_context)

        # Which copy wins, and how fast, is the only way to tell a working race
        # from two requests serialised behind the same connection pool.
        started = time.perf_counter()
        winner: asyncio.Future = asyncio.get_running_loop().create_future()
        entrants: list[asyncio.Task] = []
        failures = 0

        async def entrant(idx: int) -> None:
            """Open one stream and claim the race on its first speakable chunk."""
            nonlocal failures
            stream = None
            try:
                stream = await self._client.chat.completions.create(**params)
                # Take the iterator ONCE and hand that same object to the
                # winner. Re-entering `async for` on the stream would only
                # resume correctly if __aiter__ is idempotent; openai's
                # AsyncStream happens to be, but a stream that hands back a
                # fresh generator would silently replay the whole reply and the
                # caller would hear the first words twice.
                stream_iter = stream.__aiter__()
                buffered = []
                async for chunk in stream_iter:
                    buffered.append(chunk)
                    if not _has_content(chunk):
                        continue
                    if winner.done():
                        break  # someone else got there first
                    winner.set_result((idx, stream, stream_iter, buffered))
                    return  # hand the live stream over, still open
                # The stream ended without ever carrying content -- an empty
                # reply. Still a valid result if nobody else has won.
                if not winner.done():
                    winner.set_result((idx, stream, stream_iter, buffered))
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[hedge] copy {idx} failed: {e!r}")
                failures += 1
                # Only surface the error if every copy has now failed; otherwise
                # a single bad socket would kill a turn a sibling could serve.
                if failures >= self._hedge and not winner.done():
                    winner.set_exception(e)
            if stream is not None:
                await _close(stream)

        for i in range(self._hedge):
            entrants.append(asyncio.create_task(entrant(i)))

        try:
            idx, stream, stream_iter, buffered = await winner
        except Exception:
            for t in entrants:
                t.cancel()
            asyncio.create_task(_reap(entrants))
            raise

        # Cancel the losers and reclaim their sockets. Their reasoning tokens are
        # already billed; what matters here is not leaking connections.
        logger.debug(
            f"[hedge] copy {idx} won after {time.perf_counter() - started:.3f}s"
        )
        losers = [t for n, t in enumerate(entrants) if n != idx]
        for t in losers:
            t.cancel()
        asyncio.create_task(_reap(losers))

        return _replay(stream, stream_iter, buffered)


async def _close(stream: Any) -> None:
    try:
        if hasattr(stream, "close"):
            await stream.close()
        elif hasattr(stream, "aclose"):
            await stream.aclose()
    except Exception:
        pass


async def _reap(tasks: list[asyncio.Task]) -> None:
    """Await cancelled entrants so their sockets close and nothing warns."""
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass


async def _replay(stream: Any, stream_iter: Any, buffered: list):
    """Yield what the winner already produced, then the rest of its stream.

    `stream_iter` is the very iterator the race was reading, not a fresh one, so
    the reply continues from where the race stopped instead of starting over.

    `_process_context` closes whatever this returns with `aclose()` and then
    `close()`. An async generator supports both, so the caller's existing
    cleanup path keeps working unchanged.
    """
    try:
        for chunk in buffered:
            yield chunk
        async for chunk in stream_iter:
            yield chunk
    finally:
        await _close(stream)
