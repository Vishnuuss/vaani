"""Runs 864 and 868: a factory said "50 to 60 lakhs" and was called a liar.

    bot : మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?
    user: 50 టు 60 లాక్స్ అట్లా వస్తుంది.
    bot : సరే, నెలకి మీ బిల్లు సుమారు ఎంత అవుతుంది?      <- asked again
    user: అదే 50 టు 60 లాక్స్.                            (the SAME thing)
    bot : సరే, అది చాలా పెద్ద బిల్లు అనిపిస్తోంది,
          నెలకి నిజంగా ఎంత వస్తుంది?                     "how much REALLY"
    user: అంతే అండి నిజంగా.                              (that IS really it)
                                                          *** hung up ***

Three of the five MB Solar callers on 11 Sep were factories or commercial sites
quoting 50-80 lakhs. They are the largest leads this business gets.

The mechanism, end to end:

  1. `parse_amount` reads the range CORRECTLY -- lo 50L, hi 60L.
  2. It then collapses it to the MIDPOINT, 55L.
  3. 55L > MAX_PLAUSIBLE (50L), so `plausible` is False.
  4. `CallState.note_amount` sets `doubted` and returns False -- the figure is
     NEVER STORED.
  5. The state block tells the model the figure "cannot be a monthly
     electricity bill" and to ask him to confirm it.
  6. `alternatives()` returns [] for a range, so the one repair this system was
     designed to offer -- "did you mean thousands, or lakhs?" -- is unavailable,
     and it falls back to the blunt challenge.

The ceiling itself is well argued and is NOT changed here: at ~Rs 8/unit,
50 lakh a month is about 860 kW drawn continuously, past what any rooftop array
serves, and run 312 proved the gate is load-bearing (2,000 rupees came back from
Sarvam as "రెండు కోట్లు" and was congratulated).

What is wrong is the MIDPOINT. A range whose LOW end is credible is a credible
range. "50 to 60 lakhs" contains a plausible reading -- 50 lakh, exactly at the
ceiling -- and the caller should be believed at the conservative end rather than
contradicted on an average he never said.

A range with no plausible reading at all ("50 to 60 crores") is still doubted.
"""

from __future__ import annotations

from api.services.vaani import amounts
from api.services.vaani.state import CallState


def test_a_range_is_judged_on_its_low_end_not_its_midpoint():
    a = amounts.parse_amount("50 టు 60 లాక్స్")
    assert a is not None and a.is_range
    assert (a.low, a.high) == (5_000_000, 6_000_000)
    assert a.plausible, (
        "50 lakh is exactly at the ceiling and is a credible industrial bill. "
        "Judging the 55-lakh midpoint instead called a real customer a liar")


def test_the_range_is_preserved_and_not_flattened_to_one_number():
    a = amounts.parse_amount("50 టు 60 లాక్స్")
    assert a.low == 5_000_000 and a.high == 6_000_000, (
        "he gave a range. Storing only a midpoint loses what he actually said")


def test_an_impossible_range_is_still_doubted():
    a = amounts.parse_amount("50 టు 60 క్రోర్స్")
    assert a is not None and a.is_range
    assert not a.plausible, (
        "50 crore a month is not an electricity bill at either end. The gate "
        "must still fire -- run 312 is why it exists")


def test_run312_is_not_regressed_by_any_of_this():
    """The single figure that justified the ceiling. Not a range, untouched."""
    a = amounts.parse_amount("రెండు కోట్లు")
    assert a is not None and not a.is_range
    assert not a.plausible
    assert a.alternatives(), (
        "a single implausible figure must still offer the scale repair -- "
        "'did you mean 2 thousand, or 2 lakh?'")


def test_the_factory_bill_is_stored_instead_of_thrown_away():
    s = CallState()
    s.required_fields = ["monthly_bill"]
    s.questions = {"monthly_bill": "నెలకి బిల్లు ఎంత?"}
    s.pending_ask = "monthly_bill"
    s.commit_ask()

    assert s.note_amount("50 టు 60 లాక్స్"), (
        "this is the biggest lead MB Solar gets. It was being challenged and "
        "then deleted from the lead record")
    assert s.known.get("monthly_bill")
    assert s.doubted is None, "he is not to be contradicted on a real figure"
