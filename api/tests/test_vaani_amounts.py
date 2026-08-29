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

from api.services.vaani.amounts import parse_amount

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
