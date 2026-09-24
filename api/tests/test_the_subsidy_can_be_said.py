"""The client's PM Surya Ghar figures must reach the caller.

Two rules could replace the subsidy answer with SAFE_FALLBACK -- "I cannot give
you the correct figure right now" -- and both were live:

  * `no_invented_quantity` fired because the client's figures were not on the
    whitelist. Fixed on 23 Sep by `whitelist_numbers` reading both prompt halves.
  * `no_price_quote` fired regardless, because it matches the WORD "rupees"
    and a figure next to it, and cannot tell a government subsidy the client
    wrote down from a price the model made up. `_gate` never passed
    `allow_price`, so it could never be satisfied.

So the second one was still blocking the strongest answer this agent has.

The rule's own exemption already states the principle. It lets a reply echo the
caller's number because "the rule exists to stop the agent INVENTING a price,
and echoing their number invents nothing". A figure from the client's own
reference answer, on the turn the model was handed that answer, invents nothing
either.

The design: on a turn where a reference row was served, a price phrase is
exempt only if it appears VERBATIM in that row. Nothing else relaxes, and an
ordinary turn is untouched.

Two designs were rejected, and the tests below pin why:

  * A blanket `allow_price` on reference turns -- tried FIRST, and unsafe. The
    reasoning was that `no_invented_quantity` would still catch an invented
    price. It does not: `invented_quantities` only examines sentences carrying a
    UNIT word, and "rupees" is not one, so the two rules partition the space.
    `test_an_invented_price_is_still_caught_on_that_same_turn` failed against it.
  * A token whitelist. `known_numbers` holds "two" and "thousand", so "two
    thousand rupees" would pass although the served row says nothing of the
    kind. `test_whitelisted_words_do_not_launder_a_new_price` pins that.
"""

from api.services.vaani import client_reference, guardrails
from api.services.vaani.brain_processor import whitelist_numbers
from api.services.vaani.reply_sanitizer import ReplySanitizer
from api.services.vaani.state import CallState

REFERENCE = """## Reference

**Is the subsidy real?**
TRIGGER: సబ్సిడ|subsid|స్కీమ
"PM Surya Ghar స్కీమ్ లో మొదటి two kW కి thirty thousand rupees అండి. ఆ తర్వాత kW కి eighteen thousand rupees. మొత్తం seventy eight thousand rupees వరకు."
"""

COMPILED = "## OUTPUT CONTRACT\n- Never invent a number. Not one.\n"

SUBSIDY = ("PM Surya Ghar స్కీమ్ లో మొదటి two kW కి thirty thousand rupees అండి. "
           "ఆ తర్వాత kW కి eighteen thousand rupees.")
INVENTED = "మీ సిస్టమ్ కి ninety nine thousand rupees అవుతుంది అండి."

QUESTIONS = {"monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?"}


def _call(caller_says: str):
    """A ReplyFilter over real state, posed on the turn the caller spoke."""
    from api.services.vaani.brain_processor import ReplyFilter

    state = CallState(required_fields=list(QUESTIONS), questions=dict(QUESTIONS))
    state.reference = client_reference.parse(client_reference.split(
        COMPILED + REFERENCE)[1])
    state.last_user_text = caller_says
    state.render()                       # this is where the row is served

    class _Injector:
        pass

    inj = _Injector()
    inj.state = state
    inj.known_numbers = whitelist_numbers(COMPILED, REFERENCE)

    f = ReplyFilter.__new__(ReplyFilter)
    f._injector = inj
    f._sanitizer = ReplySanitizer()
    f._spoken = ""
    f._blocked = False
    f._said = []
    f._stale = False
    f._pending_repeat = ""
    f._repair_suppressed = False
    return f, state


def test_the_state_records_that_a_reference_row_was_served():
    _, state = _call("సబ్సిడీ ఎంత వస్తుంది?")
    assert state.reference_served_this_turn is True


def test_an_ordinary_turn_serves_nothing():
    _, state = _call("సొంత ఇల్లు అండి")
    assert state.reference_served_this_turn is False


def test_the_flag_does_not_leak_into_the_next_turn():
    """A per-turn fact. If it stuck, one subsidy question would license prices
    for the rest of the call."""
    _, state = _call("సబ్సిడీ ఎంత వస్తుంది?")
    assert state.reference_served_this_turn is True
    state.last_user_text = "సొంత ఇల్లు అండి"
    state.render()
    assert state.reference_served_this_turn is False


def test_the_subsidy_answer_is_spoken_when_asked():
    """The defect. This used to be replaced with SAFE_FALLBACK."""
    f, _ = _call("సబ్సిడీ ఎంత వస్తుంది?")
    out = f._gate(SUBSIDY)
    assert out != guardrails.SAFE_FALLBACK, (
        "the client's own subsidy figure was refused as an invented price")
    assert "thirty thousand" in out


def test_an_invented_price_is_still_caught_on_that_same_turn():
    """The layer that makes relaxing the price rule safe."""
    f, _ = _call("సబ్సిడీ ఎంత వస్తుంది?")
    assert f._gate(INVENTED) in (guardrails.SAFE_FALLBACK, guardrails.SAFE_CLOSE), (
        "ninety nine thousand is in no reference row and must not be said")


def test_a_price_on_an_ordinary_turn_is_still_blocked():
    """No reference row served, so the price rule applies in full."""
    f, _ = _call("సొంత ఇల్లు అండి")
    assert f._gate(SUBSIDY) in (guardrails.SAFE_FALLBACK, guardrails.SAFE_CLOSE)


def test_whitelisted_words_do_not_launder_a_new_price():
    """The trap a token whitelist falls into. "two" and "thousand" are both in
    the client's file, so a token check passes "two thousand rupees" -- a price
    appearing nowhere in the served row. Only a phrase-exact check refuses it."""
    known = whitelist_numbers(COMPILED, REFERENCE)
    assert {"two", "thousand"} <= known, "premise: both words are whitelisted"
    f, _ = _call("సబ్సిడీ ఎంత వస్తుంది?")
    laundered = "మీ సిస్టమ్ కి two thousand rupees అవుతుంది అండి."
    assert f._gate(laundered) in (guardrails.SAFE_FALLBACK, guardrails.SAFE_CLOSE)


def test_the_price_rule_does_not_rely_on_the_quantity_rule():
    """Why a blanket allow_price was unsafe, pinned: `invented_quantities` only
    examines sentences with a UNIT word, and "rupees" is not one. So on its own
    it cannot see an invented rupee amount at all."""
    known = whitelist_numbers(COMPILED, REFERENCE)
    r = guardrails.check(INVENTED, known_numbers=known, allow_price=True)
    assert not any(v.rule == "no_invented_quantity" for v in r.violations), (
        "if this ever starts firing, the partition changed and the design "
        "note in guardrails._quoted_from needs revisiting")
