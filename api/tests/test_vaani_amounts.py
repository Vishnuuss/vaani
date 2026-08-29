"""Money is read by code, not guessed by a model.

Run 274 is why this module exists:

    AGENT   సరే, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా ఉంటుంది?
    CALLER  వన్ లాక్ అండి
    AGENT   సరే, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా ఉంటుంది?
    CALLER  వన్ లాక్ అండి
    AGENT   సరే, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా ఉంటుంది?

Stored: monthly_bill null. He answered clearly, twice, in the way these callers
always answer -- an English numeral and an English scale word written in Telugu
script -- and the agent could not find a number in it.

Every case below is a verbatim caller utterance from a real call on this
project. They are the specification.
"""

from __future__ import annotations

import pytest

from api.services.vaani.amounts import Amount, parse_amount

# (utterance, rupees, which run it came from)
REAL = [
    ("వన్ లాక్ అండి", 100_000, "274 -- the failure"),
    ("పది లక్షలు అండి.", 1_000_000, "269"),
    ("ఆ ఓకే. 10 టు 15 లాక్స్ అట్లా వస్తుందండి.", 1_250_000, "262"),
    ("టెన్ టు ట్వంటీ లాక్స్", 1_500_000, "218"),
    ("ఉంటది ఫిఫ్టీ క్రోర్స్ అట్లా ఉంటది.", 500_000_000, "271"),
    ("టెన్ థౌసండ్ అండి.", 10_000, "272"),
    ("₹50,000.", 50_000, "273"),
    ("మూడు లక్షలు", 300_000, "266"),
]


@pytest.mark.parametrize("said,rupees,run", REAL)
def test_every_real_caller_utterance_parses(said, rupees, run):
    got = parse_amount(said)
    assert got is not None, f"no amount found in {said!r} (from run {run})"
    assert got.rupees == rupees


@pytest.mark.parametrize("said,rupees", [
    ("ఒక లక్ష", 100_000),
    ("రెండు వేలు", 2_000),
    ("ఫిఫ్టీ థౌజండ్", 50_000),
    ("ట్వంటీ ఫైవ్ థౌజండ్", 25_000),      # compound: 25, not 5
    ("twenty five thousand", 25_000),
    ("50000", 50_000),
])
def test_the_forms_these_callers_use(said, rupees):
    assert parse_amount(said).rupees == rupees


def test_a_compound_numeral_is_not_read_as_its_last_word():
    """"ట్వంటీ ఫైవ్ థౌజండ్" read as 5,000 is a twentyfold error, silently."""
    assert parse_amount("ట్వంటీ ఫైవ్ థౌజండ్").rupees == 25_000


# --- what must NOT be read as money ------------------------------------------


@pytest.mark.parametrize("said", [
    "రేపు ఉదయం పది గంటలకు",          # a time
    "మాకు ఐదు కిలోవాట్ కావాలి",       # kilowatts
    "ఇంత రుపీస్ ఉంది.",               # no figure at all
    "ఆ చెప్పండి.",
    "మాది కంపెనీ.",
    "",
])
def test_things_that_are_not_amounts(said):
    """The mirror of the booking bug, where "మూడు లక్షలు" became 3 oclock."""
    assert parse_amount(said) is None


def test_a_bare_small_number_is_not_an_amount():
    """A stray "2" in a sentence is not a bill; guessing costs a re-ask."""
    assert parse_amount("2") is None


# --- ranges ------------------------------------------------------------------


def test_a_range_is_kept_as_a_range():
    """Run 218's caller gave a range and was asked three more times for one
    figure. The midpoint lets the agent accept the answer and move on."""
    a = parse_amount("టెన్ టు ట్వంటీ లాక్స్")
    assert a.is_range
    assert (a.low, a.high) == (1_000_000, 2_000_000)
    assert a.rupees == 1_500_000


def test_a_shared_scale_word_applies_to_both_figures():
    """"టెన్ టు ట్వంటీ లాక్స్" has ONE scale word covering both numbers."""
    a = parse_amount("టెన్ టు ట్వంటీ లాక్స్")
    assert a.low == 1_000_000, "the first figure lost its scale"


# --- reading it back ---------------------------------------------------------


@pytest.mark.parametrize("rupees,expected", [
    (100_000, "1 లక్షలు"),
    (2_000, "2 వేలు"),
    (50_000_000, "5 కోట్లు"),
])
def test_it_can_say_the_amount_back(rupees, expected):
    """The caller is the only one who can catch a misheard figure."""
    from api.services.vaani.amounts import Amount
    assert Amount(rupees=rupees).say() == expected


# --- run 286: an impossible figure was celebrated -----------------------------
#
#   CALLER  60 క్రోర్స్ ఉంటాయి.
#   AGENT   మంచిది సార్, 60 కోట్లు బిల్లు నిజంగా పెద్దది, ఇలాంటి బిల్లు ఉన్నప్పుడు
#           సోలార్ పెట్టడం వేగంగా లాభం ఇస్తుంది.
#
# Six hundred million rupees a month. No such electricity bill exists. The agent
# agreed with it enthusiastically, and the saved record then read
# `monthly_bill: 60` -- three different numbers for one sentence, none of them
# real. A salesperson who accepts that figure has stopped listening.


@pytest.mark.parametrize("said,rupees", [
    ("60 క్రోర్స్ ఉంటాయి.", 600_000_000),
    ("వంద కోట్లు", 1_000_000_000),
])
def test_an_impossible_bill_is_heard_but_not_believed(said, rupees):
    a = parse_amount(said)
    assert a is not None, "it was said, so it must still be parsed"
    assert a.rupees == rupees
    assert a.plausible is False


@pytest.mark.parametrize("said", [
    "యాభై లక్షలు",       # 50 lakhs -- a genuine large factory
    "వన్ లాక్ అండి",
    "రెండు వేలు",
    "టెన్ థౌసండ్ అండి.",
])
def test_real_bills_are_still_believed(said):
    """A real large bill must never be doubted -- Rs 50 lakh a month is a
    genuine large factory and the best lead this agent can get."""
    assert parse_amount(said).plausible is True


def test_the_ceiling_admits_a_very_large_factory():
    """Rewritten after run 312, where the old ceiling let 2 crore through.

    The old assertion read `MAX_PLAUSIBLE >= 10_000_000` while its own message
    said "10 lakhs a month must remain credible". Those are not the same number
    -- 10 lakh is 1,000,000 -- so the test was enforcing a bound ten times
    looser than the reason it gave for it. The stated intent is what is
    asserted here now, plus the ceiling that intent actually implies.

    At the ~Rs 8/unit industrial HT tariff, Rs 50 lakh a month is roughly 860 kW
    drawn continuously, already at the top of what a rooftop array can serve.
    Above that the caller is not this company's customer even when the figure is
    real, so asking him to confirm it costs nothing.
    """
    from api.services.vaani.amounts import MAX_PLAUSIBLE
    assert Amount(rupees=1_000_000).plausible, "10 lakhs a month is credible"
    assert Amount(rupees=5_000_000).plausible, "50 lakhs a month is credible"
    assert not Amount(rupees=20_000_000).plausible, (
        "2 crore a month is what run 312 recorded from a caller saying 2,000")
    assert MAX_PLAUSIBLE == 5_000_000
