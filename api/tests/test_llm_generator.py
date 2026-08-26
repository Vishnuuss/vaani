"""The speculative generation call itself.

Reuses the LLM service's own OpenAI-compatible client, which matters twice:

* it is the **same warm connection pool** the pipeline already uses — the bench
  measured connection reuse as worth ~600 ms on first token;
* messages are assembled by `build_turn_messages`, so the system prompt stays a
  byte-identical cacheable prefix and the per-turn state block sits after the
  history. The last real call showed `cache_read_input_tokens: 0` against 8,384
  prompt tokens, so the prefix has to stay stable or every speculation pays
  full price.
"""

import pytest

from api.services.pipecat.speculation.llm_generator import make_llm_generator


class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeClient:
    """Minimal stand-in for the OpenAI async client."""

    def __init__(self, tokens):
        self._tokens = tokens
        self.calls: list[dict] = []
        self.chat = self

    @property
    def completions(self):
        return self

    async def create(self, **params):
        self.calls.append(params)

        async def _stream():
            for t in self._tokens:
                yield _Chunk(t)
            yield _Chunk(None)  # providers send a final empty delta

        return _stream()


class FakeSettings:
    model = "test-model"


class FakeLLMService:
    def __init__(self, client):
        self._client = client
        self._settings = FakeSettings()


@pytest.mark.asyncio
async def test_it_streams_tokens_from_the_llm():
    client = FakeClient(["అలాగే ", "అండి"])
    generate = make_llm_generator(
        FakeLLMService(client),
        system_prompt="You are Priya.",
        get_history=lambda: [],
        get_state_block=lambda: "",
    )

    tokens = [t async for t in generate("అవును")]

    assert "".join(tokens) == "అలాగే అండి"


@pytest.mark.asyncio
async def test_the_system_prompt_is_the_cacheable_first_message():
    client = FakeClient(["ok"])
    generate = make_llm_generator(
        FakeLLMService(client),
        system_prompt="You are Priya.",
        get_history=lambda: [{"role": "user", "content": "hi"}],
        get_state_block=lambda: "still to find out: bill",
    )

    _ = [t async for t in generate("అవును")]

    messages = client.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "You are Priya."}
    assert messages[-1] == {"role": "user", "content": "అవును"}
    # state block must sit after the history, never in the cached prefix
    assert "still to find out" in str(messages[-2]["content"])


@pytest.mark.asyncio
async def test_it_streams_rather_than_waiting_for_the_whole_completion():
    client = FakeClient(["a", "b"])
    generate = make_llm_generator(
        FakeLLMService(client),
        system_prompt="p",
        get_history=lambda: [],
        get_state_block=lambda: "",
    )

    _ = [t async for t in generate("x")]

    assert client.calls[0]["stream"] is True
    assert client.calls[0]["model"] == "test-model"
