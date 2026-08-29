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
import time

from loguru import logger

# Long enough for a cold connection and a one-token reply, short enough that the
# task is always gone before it could overlap the caller's first answer.
TIMEOUT_S = 8.0

# The benchmark makes several requests in one session, so it gets its own,
# larger budget. It runs after the warm-up and blocks nothing.
BENCH_TIMEOUT_S = 30.0

# The reply is thrown away; only the cached prefix is wanted.
MAX_TOKENS = 1


async def _warm(api_key: str, model: str, system_prompt: str, base_url: str,
                report=None) -> None:
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
    started = time.monotonic()
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
    wall = time.monotonic() - started

    # How much of the LLM's time is the network rather than the model.
    #
    # Groq reports its own timings in `usage`. Subtracting them from the wall
    # clock leaves the round trip, and that number decides whether the LLM is
    # slow or merely far away: this server is in Mumbai and api.groq.com
    # resolves to Toronto, which would put a transcontinental hop on every turn
    # of every call. It has never been measured, so it has never been fixed.
    server = queue = compute = 0.0
    try:
        usage = (json.loads(body).get("usage") or {})
        queue = float(usage.get("queue_time") or 0.0)
        compute = (float(usage.get("prompt_time") or 0.0)
                   + float(usage.get("completion_time") or 0.0))
        server = queue + compute
    except Exception:
        pass
    network = max(0.0, wall - server)
    logger.info(f"[prewarm] cached {len(system_prompt):,} chars; "
                f"wall {wall:.3f}s, provider {server:.3f}s, network {network:.3f}s")
    if report:
        try:
            await report({"type": "rtf-prewarm", "payload": {
                "wall_secs": round(wall, 4),
                "queue_secs": round(queue, 4),
                "compute_secs": round(compute, 4),
                "provider_secs": round(server, 4),
                "network_secs": round(network, 4),
                "prompt_chars": len(system_prompt),
            }})
        except Exception:
            pass


# Candidates for the routine-turn model, measured against the live prompt on the
# live server. gpt-oss-120b is what runs today; the rest are smaller and should
# be faster. Which of them is fast ENOUGH and good enough is not a guess to be
# made from a datasheet -- it is measured here, on the real prompt, from the
# machine that will actually call it.
BENCH_MODELS = ("openai/gpt-oss-20b",)

# Prompt sizes to time, as a fraction of the live prompt.
#
# Run 281 killed the model-swap theory: gpt-oss-20b answered in 0.414s of
# provider time against the 120b's 0.399s -- the smaller model is not faster
# here. Both were processing the same 28,850-character prompt, which points at
# the prompt rather than the model.
#
# Prompt size was dismissed earlier on the grounds that 85% of it is served from
# the provider's cache and therefore nearly free. That reasoning was about COST.
# A cache read is not free in TIME, and this measures whether it is the term
# that matters.
# Run 284 showed length does not predict the time, so the sweep now repeats
# ONE size several times: the thing being measured is variance.
BENCH_PROMPT_FRACTIONS = (1.0, 1.0, 1.0, 1.0, 1.0)


async def _bench_one(session, api_key, model, system_prompt, base_url):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": "."}],
        "max_completion_tokens": MAX_TOKENS,
    }
    t0 = time.monotonic()
    async with session.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        data=json.dumps(payload),
    ) as resp:
        body = await resp.text()
    wall = time.monotonic() - t0
    if resp.status != 200:
        return {"model": model, "error": body[:90]}
    usage = (json.loads(body).get("usage") or {})
    # Queue and compute kept APART. Summing them hid the whole story on run 284:
    # the same prompt on the same model, seconds apart, reported 0.080s and
    # then 0.413s. Prompt length explained none of it, so the variance is either
    # waiting for a worker or the worker itself, and one number cannot say which.
    queue = float(usage.get("queue_time") or 0)
    compute = (float(usage.get("prompt_time") or 0)
               + float(usage.get("completion_time") or 0))
    return {
        "model": model,
        "wall_secs": round(wall, 4),
        "queue_secs": round(queue, 4),
        "compute_secs": round(compute, 4),
        "provider_secs": round(queue + compute, 4),
    }


async def benchmark_models(api_key: str, system_prompt: str, report=None,
                           base_url: str = "https://api.groq.com/openai/v1") -> list:
    """Time the candidate models once, on the real prompt, from this server.

    Runs at most once per call and is capped at one token each, so the cost is
    a rounding error against the call itself. Failures are reported, not raised:
    a model the account cannot reach is a fact worth recording, not an outage.
    """
    import aiohttp

    out = []
    timeout = aiohttp.ClientTimeout(total=BENCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model in BENCH_MODELS:
            try:
                out.append(await _bench_one(session, api_key, model,
                                            system_prompt, base_url))
            except Exception as e:
                out.append({"model": model, "error": repr(e)[:90]})

        # Does the PROMPT decide the time rather than the model? Same model,
        # same server, same moment -- only the length changes.
        live = BENCH_MODELS[0]
        for frac in BENCH_PROMPT_FRACTIONS:
            cut = system_prompt[: max(200, int(len(system_prompt) * frac))]
            try:
                r = await _bench_one(session, api_key, live, cut, base_url)
                r["prompt_chars"] = len(cut)
                r["fraction"] = frac
                out.append(r)
            except Exception as e:
                out.append({"fraction": frac, "error": repr(e)[:90]})

    logger.info(f"[bench] {out}")
    if report:
        try:
            await report({"type": "rtf-model-bench", "payload": {"models": out}})
        except Exception:
            pass
    return out


def prewarm_prompt_cache(
    api_key: str | None,
    model: str | None,
    system_prompt: str | None,
    *,
    base_url: str = "https://api.groq.com/openai/v1",
    report=None,
    bench: bool = False,
) -> asyncio.Task | None:
    """Fire the warming request and return immediately.

    Returns the task so a test can await it. Callers on the voice path must NOT
    await it -- the whole point is that the greeting covers the wait.
    """
    if not (api_key and model and system_prompt):
        return None

    async def run() -> None:
        try:
            await _warm(api_key, model, system_prompt, base_url, report)
            if bench:
                # After the warm-up, never before it: the live model's cache
                # must be primed first, and the benchmark must not delay it.
                await benchmark_models(api_key, system_prompt, report, base_url)
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
