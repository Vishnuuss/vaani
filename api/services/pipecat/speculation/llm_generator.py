"""Builds the speculative generation call.

Reuses the LLM service's own OpenAI-compatible client rather than opening a new
one. That is deliberate: the bench measured connection reuse as worth ~600 ms on
first token, so a speculation on a cold connection would eat most of what
speculating is meant to save.

Messages come from `build_turn_messages`, which keeps the system prompt as a
byte-identical first message. The last real call recorded
`cache_read_input_tokens: 0` against 8,384 prompt tokens — every turn paying to
reprocess the whole prompt — so prefix stability is not cosmetic here.
"""

from typing import AsyncIterator, Callable, Sequence

from loguru import logger

from api.services.workflow.turn_prompt import build_turn_messages


def make_llm_generator(
    llm,
    system_prompt: str,
    get_history: Callable[[], Sequence[dict]],
    get_state_block: Callable[[], str],
):
    """Return an async callable: speculated user text -> token stream.

    Args:
        llm: The pipeline's LLM service (OpenAI-compatible; exposes ``_client``
            and ``_settings.model``).
        system_prompt: The compiled agent prompt — the cacheable prefix.
        get_history: Returns the conversation so far at call time.
        get_state_block: Returns the per-turn state (e.g. outstanding
            questions), placed after the history so it never breaks the prefix.
    """

    async def generate(text: str) -> AsyncIterator[str]:
        messages = build_turn_messages(
            system_prompt, list(get_history()), get_state_block(), text
        )

        stream = await llm._client.chat.completions.create(
            model=llm._settings.model,
            messages=messages,
            stream=True,
        )

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def guarded(text: str) -> AsyncIterator[str]:
        """Never let a speculative failure surface as a pipeline error."""
        try:
            async for token in generate(text):
                yield token
        except Exception as e:
            logger.debug(f"[speculation] generation aborted: {e}")
            return

    return guarded
