"""Run 890 (11 Sep): the same two slots, five times, and the call died on it.

    t9   BOT  ...either tomorrow at ten o'clock లేదా Thursday at two...
    t10  BOT  ...tomorrow at ten o'clock లేదా Thursday at two...
    t11  BOT  ...either tomorrow at ten o'clock లేదా Thursday at two...
    t12  BOT  ...tomorrow at ten o'clock లేదా Thursday at two...
    t13  BOT  ...tomorrow at ten o'clock లేదా Thursday at two...
    USER మీరంత కూడా ఆడుకుంటారు సార్.        you are all just playing games

He had already agreed -- "ఆ తప్పకుండా" (yes, certainly) -- and given every
field. The call captured all six and still ended badly.

`MAX_ASKS_PER_FIELD` did not bind because an OFFER is not an ask: it goes out
through `offer_line()` from the `buying_signal` / `next_step_agreed` branches,
and `closing_is_due` deliberately excludes "offering times" from counting as a
close, on the grounds that naming two slots is a question rather than a
goodbye. That reasoning is right and left the offer itself unbounded.

Run 318 is the same shape and its lesson was written down at the time: "an
agent with nothing left to ask, asking the last thing again." It was bounded
for questions and never for offers.

Two offers is the whole budget: one to name the times, one to repeat them if he
missed them. After that the times stop being put to him and the visit is left
to be arranged by a human -- an appointment he never chose is worth less than a
lead who was not harassed.
"""

from api.services.vaani.state import CallState


def _ready() -> CallState:
    """Everything answered, he has agreed, no time fixed yet."""
    s = CallState(required_fields=["monthly_bill"],
                  questions={"monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత?"})
    s.known["monthly_bill"] = "60000"
    s.next_step_agreed = True
    return s


def test_the_two_times_may_be_put_to_him_twice():
    s = _ready()
    first = s.offer_line()
    second = s.offer_line()
    assert first and second
    assert s.offers_made == 2


def test_run_890_a_third_offer_is_refused():
    s = _ready()
    s.offer_line()
    s.offer_line()
    third = s.offer_line()
    assert "ten o'clock" not in third.lower() or "already" in third.lower(), (
        "the same two slots went out a third time -- run 890 did it five times")
    assert s.offers_made == 2, "the budget must not keep climbing"


def test_once_spent_the_state_block_stops_offering_times():
    s = _ready()
    s.offer_line()
    s.offer_line()
    block = s.render()
    assert "OFFERED TWICE" in block
    assert "do NOT offer" in block


def test_choosing_a_slot_is_unaffected():
    """The budget bounds OFFERS, never his ability to accept one."""
    s = _ready()
    s.offer_line()
    assert s.offered, "the slots must still exist for him to choose from"


def test_a_fresh_call_still_offers_normally():
    s = _ready()
    assert s.offers_made == 0
    line = s.offer_line()
    assert line
    assert s.offers_made == 1
