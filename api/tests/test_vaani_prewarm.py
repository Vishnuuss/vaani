"""Warming the prompt cache must never be able to hurt a call.

Measured across eight real calls: the LLM's first token on the FIRST turn takes
0.674s at the median against 0.281s on every later turn, with a tail of 1.55s,
1.73s and 1.31s. Same model, same prompt -- the difference is that the prefix is
not cached yet. Run 262 served 219,648 of 258,792 input tokens from cache once
under way, and none of them on turn one.

The greeting takes about four seconds to speak, so there is room to warm it for
free. The risk is not that it fails to help; it is that a background request
somehow delays or breaks the call. Everything below tests that it cannot.
"""

from __future__ import annotations

import asyncio

import pytest

from api.services.vaani.prewarm import MAX_TOKENS, prewarm_prompt_cache


def test_missing_credentials_do_nothing():
    """A workflow on another provider, or with no key, must simply skip."""
    assert prewarm_prompt_cache(None, "model", "prompt") is None
    assert prewarm_prompt_cache("key", None, "prompt") is None
    assert prewarm_prompt_cache("key", "model", None) is None
    assert prewarm_prompt_cache("", "", "") is None


def test_it_returns_without_blocking():
    """The greeting covers the wait only if the caller never waits on this."""
    async def main():
        task = prewarm_prompt_cache("k", "m", "prompt", base_url="http://127.0.0.1:1")
        assert isinstance(task, asyncio.Task)
        assert not task.done(), "prewarm must not run inline"
        await asyncio.wait_for(task, timeout=10)
        return task
    task = asyncio.run(main())
    assert task.exception() is None, "a failed warm-up must not raise"


def test_an_unreachable_provider_is_swallowed():
    """A cold first turn is the status quo, not a fault worth escalating."""
    async def main():
        task = prewarm_prompt_cache("k", "m", "p", base_url="http://127.0.0.1:1")
        await asyncio.wait_for(task, timeout=10)
        assert task.exception() is None
    asyncio.run(main())


def test_outside_an_event_loop_it_declines_rather_than_raising():
    assert prewarm_prompt_cache("k", "m", "p") is None


def test_only_one_token_is_requested():
    """The reply is thrown away; only the cached prefix is wanted."""
    assert MAX_TOKENS == 1
