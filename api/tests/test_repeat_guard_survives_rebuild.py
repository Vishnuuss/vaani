"""The repeat guard must see what earlier TURNS said, not just this pipeline's.

`_is_repeat` compared the candidate reply against `self._said`, a list held on
the ReplyFilter instance. On a phone call that object lives for the whole call,
so the guard worked. In text chat `text_chat_runner` builds a fresh pipeline for
every message, so `_said` was always empty and the guard could never fire.

Measured on run 721, after the ask budget was already correct
(`ask_counts: {"loan_type": 2}` -- capped at two, exactly as designed):

    "సరే అండి, ఏ loan అండి?"
    "మంచిది అండి. ఏ loan అండి?"
    "అది మా team చూసుకుంటారు అండి. ఏ loan అండి?"
    "ఏ loan అండి?"

Four asks of one question. The budget stopped the STATE from requesting it;
nothing stopped the MODEL from copying it out of the conversation history. That
is what this guard is for, and it was blind.

`state.asked` is persisted across turns, so the guard reads both.
"""

from __future__ import annotations

from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.state import CallState


class _Injector:
    """The shape ReplyFilter actually uses: `.state`."""

    def __init__(self, state):
        self.state = state


def _filter_with_history(previous: list[str]) -> ReplyFilter:
    st = CallState(required_fields=["loan_type"],
                   questions={"loan_type": "ఏ loan అండి?"})
    st.asked.extend(previous)
    return ReplyFilter(_Injector(st))


# Verbatim from run 721.
ASKED_BEFORE = [
    "సరే అండి, ఏ loan అండి?",
    "మంచిది అండి. ఏ loan అండి?",
    "అది మా team చూసుకుంటారు అండి. ఏ loan అండి?",
]


def test_a_repeat_is_caught_on_a_freshly_built_pipeline():
    """The run 721 case: no in-memory history, all of it in the state."""
    rf = _filter_with_history(ASKED_BEFORE)
    assert rf._is_repeat("ఏ loan అండి?")


def test_a_reworded_repeat_is_still_caught():
    """Similarity, not equality -- the model rewords while asking the same."""
    rf = _filter_with_history(ASKED_BEFORE)
    assert rf._is_repeat("మంచిది, ఏ loan అండి?")


def test_a_genuinely_new_question_is_not_a_repeat():
    """A false positive replaces a good reply with 'I could not hear you'."""
    rf = _filter_with_history(ASKED_BEFORE)
    assert not rf._is_repeat("మీ నెల ఆదాయం ఎంత వస్తుంది అండి?")


def test_an_empty_history_flags_nothing():
    rf = _filter_with_history([])
    assert not rf._is_repeat("ఏ loan అండి?")


def test_no_injector_is_still_safe():
    """A bare ReplyFilter is constructed when the brain fails to set up."""
    rf = ReplyFilter()
    assert not rf._is_repeat("ఏ loan అండి?")


def test_the_in_memory_history_still_works():
    """The voice path relies on it and must not be traded away."""
    rf = ReplyFilter()
    rf._said.append("ఏ loan అండి?")
    assert rf._is_repeat("ఏ loan అండి?")


def test_the_ask_is_charged_in_exactly_one_place():
    """Run 783: a question interrupted once burned its whole two-ask budget.

        BOT : సరే, మీరు ఏ ఏరియా లేదా సిటీలో    <- barge-in, cut mid-question
        BOT : సరే, మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?

    The caller was never asked where he lives again and the saved lead record
    has `location: null`. The end frame charged the ask, the barge-in re-rendered
    the state block and put the same field back into `pending_ask`, and the next
    reply's START frame charged it again. Two charges is the whole budget.

    `commit_ask` is idempotent by itself -- it clears `pending_ask` -- so this
    only bites when a render lands between the two commits, which is exactly
    what an interruption causes. The fix is to charge in one place, and the
    right place is the end of the reply.
    """
    import inspect

    from api.services.vaani import brain_processor

    src = inspect.getsource(brain_processor.ReplyFilter.process_frame)
    assert src.count("state.commit_ask()") == 1, (
        "the ask must be charged in exactly one place in process_frame")

    start, end = src.index("LLMFullResponseStartFrame"), src.index(
        "LLMFullResponseEndFrame")
    assert start < src.index("state.commit_ask()"), "unexpected ordering"
    assert end < src.index("state.commit_ask()"), (
        "commit_ask must sit in the END branch, not the START branch")
