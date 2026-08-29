"""Warm the provider's prompt cache while the greeting is still playing.

The measurement
---------------
Across eight real calls, the LLM's time-to-first-token on the FIRST turn of a
call against every later turn:

    first turn        median 0.674s      worst 1.551s, 1.727s, 1.313s
    every later turn  median 0.281s      n = 119

The first turn costs an extra 0.393s at the median and over a second at the
tail. It is not a slower model or a longer prompt -- it is the same prompt,
uncached. Run 262's usage record shows 219,648 of 258,792 input tokens served
from cache once the call is under way; on turn one that figure is zero.

Why this is nearly free
-----------------------
The agent opens every call with a fixed greeting that takes about four seconds
to speak, and during those four seconds nothing is being asked of the model.
Sending the system prompt then, with a one-token cap, puts the ~7,000-token
prefix into the provider's cache before the caller has finished hearing hello.

The request is billed once at uncached rates and every turn afterwards reads the
cache -- which is what the second turn onwards already does today. It buys the
first turn the same treatment.

It is the FIRST turn that matters most, too: it is the caller's first impression
of whether they are talking to something responsive, and it is the turn most
likely to be judged.

Failure is not a failure
------------------------
Nothing waits on this. It runs detached, it is capped by a short timeout, and
every error path is swallowed after a log line. If it does not finish, or the
provider ignores it, the call behaves exactly as it does today -- one slow first
turn. There is no path here that can delay or break a call.
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

# Long enough for a cold connection and a one-token reply, short enough that the
# task is always gone before it could overlap the caller's first answer.
TIMEOUT_S = 8.0

# The reply is thrown away; only the cached prefix is wanted.
MAX_TOKENS = 1


async def _warm(api_key: str, model: str, system_prompt: str, base_url: str) -> None:
    import aiohttp

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            # A user turn is required by the API. Deliberately not Telugu and
            # deliberately trivial: this must not look like a real turn to
            # anything reading the provider's logs.
            {"role": "user", "content": "."},
        ],
        "max_completion_tokens": MAX_TOKENS,
    }
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            data=json.dumps(payload),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                logger.info(f"[prewarm] provider returned {resp.status}: {body[:160]}")
                return
    logger.info(f"[prewarm] cached {len(system_prompt):,} chars of prompt "
                "before the first turn")


def prewarm_prompt_cache(
    api_key: str | None,
    model: str | None,
    system_prompt: str | None,
    *,
    base_url: str = "https://api.groq.com/openai/v1",
) -> asyncio.Task | None:
    """Fire the warming request and return immediately.

    Returns the task so a test can await it. Callers on the voice path must NOT
    await it -- the whole point is that the greeting covers the wait.
    """
    if not (api_key and model and system_prompt):
        return None

    async def run() -> None:
        try:
            await _warm(api_key, model, system_prompt, base_url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A cold first turn is the status quo, not a fault. Never escalate.
            logger.info(f"[prewarm] skipped: {e!r}")

    try:
        return asyncio.create_task(run())
    except RuntimeError:
        # No running loop (a synchronous caller, or a test). Not worth raising
        # over: the only cost is the first turn staying as slow as it is now.
        return None
