"""Cache-stable turn assembly.

Message order is the whole point:

    [0]  system  - the compiled agent prompt. Byte-identical every turn, so the
                   provider's prefix cache hits. NOTHING per-turn may go here.
    ...  history - the conversation so far.
    [-2] system  - per-turn state (e.g. which questions are still unanswered).
                   Placed AFTER the history so it is the freshest instruction
                   the model sees, and so it cannot disturb the cached prefix.
    [-1] user    - what the caller just said.

Dograh's node graph swapped the system prompt on every node transition, which
missed the cache every time. One prompt plus a trailing state block keeps the
prefix stable while still steering the turn.
"""

from typing import Any, Mapping, Sequence

Message = dict[str, Any]


def build_turn_messages(
    system_prompt: str,
    history: Sequence[Message],
    state_block: str,
    user_text: str,
) -> list[Message]:
    """Assemble one turn's messages with a cache-stable prefix."""
    messages: list[Message] = [{"role": "system", "content": system_prompt}]
    messages.extend(dict(m) for m in history)

    if state_block and state_block.strip():
        messages.append({"role": "system", "content": state_block})

    messages.append({"role": "user", "content": user_text})
    return messages


def remaining_fields_block(
    required: Sequence[str],
    gathered: Mapping[str, Any],
) -> str:
    """Name the questions still unanswered.

    This replaces the ordering guarantee the node graph gave for free. A field
    counts as answered only when it holds a real value — present-but-None means
    it was asked about and not captured, so it stays outstanding.
    """
    outstanding = [
        field
        for field in required
        if gathered.get(field) in (None, "", [], {})
    ]
    if not outstanding:
        return ""

    return (
        "Still to find out, before the call ends: "
        + ", ".join(outstanding)
        + ". Ask about these naturally, one at a time — never as a list."
    )
