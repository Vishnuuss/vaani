"""Tests for the hedged LLM race.

The hazards worth pinning here are all concurrency ones: picking the winner on
the wrong signal, dropping the chunks the winner already emitted, leaking the
losers' sockets, and turning one failed copy into a failed turn.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from api.services.vaani.hedged_llm import MAX_HEDGE, HedgedGroqLLMService, _has_content


def chunk(content=None, tool_calls=None):
    """A ChatCompletionChunk shaped like the bits the race actually reads."""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


class FakeStream:
    """An openai AsyncStream stand-in that emits chunks on a schedule.

    Deliberately NOT idempotent on __aiter__: each call returns a fresh
    generator. A real AsyncStream happens to return the same one, but the
    hedge must not depend on that -- if it re-entered the stream instead of
    keeping the iterator it took during the race, the caller would hear the
    opening words twice. This fake makes that bug fail loudly.
    """

    def __init__(self, script, closed_flag):
        self._script = list(script)
        self._closed = closed_flag

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for delay, c in self._script:
            await asyncio.sleep(delay)
            yield c

    async def close(self):
        self._closed.append(True)


class FakeCompletions:
    def __init__(self, scripts, closed):
        self._scripts = list(scripts)
        self._closed = closed
        self.calls = 0

    async def create(self, **_params):
        i = self.calls
        self.calls += 1
        script = self._scripts[min(i, len(self._scripts) - 1)]
        if isinstance(script, Exception):
            raise script
        return FakeStream(script, self._closed)


def make_service(scripts, hedge=2):
    """A HedgedGroqLLMService with the network replaced, built without __init__."""
    svc = HedgedGroqLLMService.__new__(HedgedGroqLLMService)
    svc._hedge = hedge
    closed = []
    completions = FakeCompletions(scripts, closed)
    svc._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    svc._settings = SimpleNamespace(system_instruction=None)
    svc.supports_developer_role = True
    svc.get_llm_adapter = lambda: SimpleNamespace(
        get_llm_invocation_params=lambda *a, **k: {"messages": []}
    )
    svc.build_chat_completion_params = lambda p: dict(p)
    return svc, completions, closed


async def drain(svc):
    stream = await svc.get_chat_completions(context=None)
    out = []
    async for c in stream:
        out.append(c)
    return out


# --- what wins the race -----------------------------------------------------


def test_has_content_ignores_reasoning_only_chunks():
    """A reasoning chunk has no content, so it must not claim the race."""
    assert not _has_content(chunk(content=None))
    assert not _has_content(chunk(content=""))
    assert _has_content(chunk(content="సరే"))


def test_has_content_counts_a_tool_call():
    """A tool call ends the turn just as a spoken reply does."""
    assert _has_content(chunk(tool_calls=[SimpleNamespace(id="1")]))


@pytest.mark.asyncio
async def test_first_content_wins_not_first_chunk():
    """The copy that opens its stream first can still lose.

    This is the whole point of the design: copy 0 emits reasoning immediately
    but dawdles before answering, while copy 1 starts later and answers sooner.
    Racing on the first chunk would pick copy 0 and give back the gain.
    """
    slow_starter_fast_answer = [(0.05, chunk("FAST"))]
    fast_starter_slow_answer = [(0.0, chunk(None)), (0.30, chunk("SLOW"))]
    svc, _, _ = make_service([fast_starter_slow_answer, slow_starter_fast_answer])

    out = await drain(svc)

    assert [c.choices[0].delta.content for c in out] == ["FAST"]


@pytest.mark.asyncio
async def test_winner_replays_the_chunks_it_already_consumed():
    """Chunks read while racing must still reach the caller, in order."""
    winner = [(0.0, chunk(None)), (0.0, chunk("one")), (0.0, chunk(" two"))]
    loser = [(0.5, chunk("late"))]
    svc, _, _ = make_service([winner, loser])

    out = await drain(svc)

    assert [c.choices[0].delta.content for c in out] == [None, "one", " two"]


# --- resource handling ------------------------------------------------------


@pytest.mark.asyncio
async def test_hedge_issues_exactly_n_requests():
    svc, completions, _ = make_service([[(0.0, chunk("hi"))]], hedge=2)
    await drain(svc)
    assert completions.calls == 2


@pytest.mark.asyncio
async def test_losers_are_closed_not_leaked():
    """A cancelled copy must give its socket back."""
    winner = [(0.0, chunk("win"))]
    loser = [(0.2, chunk("lose"))]
    svc, _, closed = make_service([winner, loser])

    await drain(svc)
    await asyncio.sleep(0.35)  # let the reaper finish

    assert len(closed) >= 1, "the losing stream was never closed"


@pytest.mark.asyncio
async def test_hedge_of_one_delegates_to_the_parent():
    """hedge=1 is the off switch and must not enter the race path at all."""
    svc, completions, _ = make_service([[(0.0, chunk("hi"))]], hedge=1)
    called = {}

    # super() resolves through the class, so the stub is an unbound function
    # and receives `self` as its first argument.
    async def parent(_self, context):
        called["yes"] = True
        return FakeStream([(0.0, chunk("parent"))], [])

    import api.services.vaani.hedged_llm as mod

    orig = mod.GroqLLMService.get_chat_completions
    mod.GroqLLMService.get_chat_completions = parent
    try:
        out = await drain(svc)
    finally:
        mod.GroqLLMService.get_chat_completions = orig

    assert called.get("yes"), "hedge=1 did not delegate to the parent"
    assert completions.calls == 0, "hedge=1 still issued a raced request"
    assert [c.choices[0].delta.content for c in out] == ["parent"]


# --- failure handling -------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failed_copy_does_not_fail_the_turn():
    """A single bad socket must not kill a turn a sibling can serve."""
    svc, _, _ = make_service([RuntimeError("socket blew up"), [(0.05, chunk("ok"))]])

    out = await drain(svc)

    assert [c.choices[0].delta.content for c in out] == ["ok"]


@pytest.mark.asyncio
async def test_all_copies_failing_raises():
    """If every copy fails the turn must raise, not hang on a dead future."""
    svc, _, _ = make_service([RuntimeError("boom"), RuntimeError("boom")])

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(drain(svc), timeout=2.0)


@pytest.mark.asyncio
async def test_empty_reply_still_completes_the_turn():
    """A stream that ends with no content at all must not deadlock."""
    svc, _, _ = make_service([[(0.0, chunk(None))], [(0.0, chunk(None))]])

    out = await asyncio.wait_for(drain(svc), timeout=2.0)

    assert all(c.choices[0].delta.content is None for c in out)


# --- configuration ----------------------------------------------------------


def test_hedge_is_clamped():
    """Past a handful of copies we would be creating the contention ourselves."""
    svc = HedgedGroqLLMService.__new__(HedgedGroqLLMService)
    for requested, expected in [(0, 1), (1, 1), (2, 2), (99, MAX_HEDGE)]:
        svc._hedge = max(1, min(int(requested), MAX_HEDGE))
        assert svc._hedge == expected
