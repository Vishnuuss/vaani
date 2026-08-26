"""Cache-stable turn assembly.

Two jobs, both measured or evidenced:

1. **Keep the prefix byte-identical.** Sarvam bills 29.28 vs 10.98 per 1M tokens
   for uncached vs cached input, and a cached prefix is also faster to first
   token. Dograh's node graph broke this by swapping the system prompt on every
   node transition. One prompt, never swapped, restores it — but only if
   nothing per-turn is allowed to creep into the front of the message list.

2. **Keep question coverage honest.** The node graph forced question order by
   construction; a single prompt does not. The remaining-questions block is what
   replaces that guarantee, and it must sit AFTER the history so it is the
   freshest instruction the model sees without disturbing the cached prefix.
"""

from api.services.workflow.turn_prompt import (
    build_turn_messages,
    remaining_fields_block,
)

SYSTEM = "You are Priya from BS Wealth Finance."
HISTORY = [
    {"role": "assistant", "content": "Namaskaram."},
    {"role": "user", "content": "Cheppandi."},
]


def test_the_system_prompt_is_the_first_message():
    messages = build_turn_messages(SYSTEM, HISTORY, "", "నాకు లోన్ కావాలి")

    assert messages[0] == {"role": "system", "content": SYSTEM}


def test_the_cached_prefix_is_identical_no_matter_what_the_state_says():
    a = build_turn_messages(SYSTEM, HISTORY, "still missing: income", "సరే")
    b = build_turn_messages(SYSTEM, HISTORY, "still missing: city, income", "సరే")

    # If per-turn state leaked into the prefix, every turn would miss the cache.
    assert a[0] == b[0]


def test_the_state_block_sits_after_the_history_not_before_it():
    messages = build_turn_messages(SYSTEM, HISTORY, "still missing: income", "సరే")

    state_index = next(
        i for i, m in enumerate(messages) if "still missing" in str(m["content"])
    )
    last_history_index = next(
        i for i, m in enumerate(messages) if m["content"] == "Cheppandi."
    )

    assert state_index > last_history_index


def test_the_user_turn_is_last():
    messages = build_turn_messages(SYSTEM, HISTORY, "still missing: income", "సరే")

    assert messages[-1] == {"role": "user", "content": "సరే"}


def test_no_state_block_is_added_when_nothing_is_outstanding():
    messages = build_turn_messages(SYSTEM, HISTORY, "", "సరే")

    # system + history + user, and nothing else. An empty state message would
    # be dead weight in the context on every single turn.
    assert len(messages) == len(HISTORY) + 2
    assert [m["role"] for m in messages] == ["system", "assistant", "user", "user"]


def test_remaining_fields_block_lists_only_the_unanswered_questions():
    block = remaining_fields_block(
        required=["monthly_income", "city", "loan_amount"],
        gathered={"city": "Hyderabad", "monthly_income": None},
    )

    assert "monthly_income" in block
    assert "loan_amount" in block
    assert "city" not in block


def test_remaining_fields_block_is_empty_once_everything_is_gathered():
    block = remaining_fields_block(
        required=["city"],
        gathered={"city": "Hyderabad"},
    )

    assert block == ""
