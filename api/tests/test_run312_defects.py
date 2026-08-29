"""Run 312: three ways one garbled word became a confident fact.

The call (WR-TEL-OUT-82803732, 51s) hit the latency target -- p50 0.774s -- and
lost the lead anyway. Every defect below is the same shape: Sarvam returned
something garbled and a layer downstream turned it into a fact instead of a
question. These are the regression tests for that shape.

Transcript, in full, for the two turns that matter:

    BOT  : సరే, మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?
    USER : నాది రెండు కోట్లు వస్తుంది.            <- he said "2,000 rupees"
    BOT  : మంచిది, రెండు కోట్లు బిల్లు చాలా పెద్దది ...

    BOT  : మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
    USER : ఫ్యాక్టర్ పైన                          <- "on the factory"
    BOT  : సరే, మీరు చెప్పినది బాగా వినిపించలేదు. ...
    USER : ఎక్కడండి ట్రాక్టర్ పైన మా ట్రాక్టర్ పైన
    BOT  : థాంక్యూ సార్, మీకు రూఫ్ లేకపోవడం వల్ల సోలార్ సాధ్యం కాదు. మంచి రోజు.
"""

import pytest

from api.services.vaani import amounts
from api.services.vaani.extractor import apply_to_state
from api.services.vaani.negation import is_negative
from api.services.vaani.state import CallState

FIELDS = ["monthly_bill", "location", "property_type", "roof_available"]

# What Sarvam actually returned on the two roof turns.
FACTORY = "ఫ్యాక్టర్ పైన"
TRACTOR = "ఎక్కడండి ట్రాక్టర్ పైన మా ట్రాక్టర్ పైన"


def state() -> CallState:
    return CallState()


# --- 7.1  the plausibility ceiling -----------------------------------------

def test_two_crore_a_month_is_not_a_bill():
    """The exact figure run 312 stored: 20,000,000."""
    assert not amounts.Amount(rupees=20_000_000).plausible


def test_the_transcript_that_did_it():
    amount = amounts.parse_amount("నాది రెండు కోట్లు వస్తుంది.")
    assert amount is not None
    assert amount.rupees == 20_000_000
    assert not amount.plausible, "this is what let the agent congratulate him"


def test_a_large_but_real_factory_bill_still_passes():
    """The ceiling must not start doubting genuine commercial leads.

    Rs 5 lakh a month is a substantial factory and an excellent solar lead.
    """
    assert amounts.parse_amount("ఐదు లక్షలు").plausible


def test_the_repair_names_the_scales_rather_than_asking_again():
    """"Are you sure?" gets the same misheard syllable back a second time.

    "Thousands or lakhs or crores?" is answered in one word, and the answer is
    a different word from the one that was misheard.
    """
    alts = amounts.parse_amount("రెండు కోట్లు").alternatives()
    assert "2 వేలు" in alts, "2,000 -- what he actually said"
    assert "2 లక్షలు" in alts


def test_a_plausible_amount_offers_no_alternatives():
    assert amounts.parse_amount("పది వేలు").alternatives() == []


# --- 7.2  a boolean must not go false without a negation --------------------

@pytest.mark.parametrize("said", [FACTORY, TRACTOR])
def test_saying_where_the_roof_is_is_not_saying_there_is_none(said):
    assert not is_negative(said), "there is no 'no' anywhere in this sentence"


@pytest.mark.parametrize("said", [FACTORY, TRACTOR])
def test_the_extractor_cannot_record_no_roof_from_it(said):
    """The defect itself. `roof_available: false` ended run 312."""
    s = state()
    apply_to_state(s, {"roof_available": False}, FIELDS, said)
    assert "roof_available" not in s.known, (
        "a garbled answer became a disqualifying fact")


def test_an_actual_no_is_still_recorded():
    """The gate must not make the agent deaf to a real refusal."""
    s = state()
    apply_to_state(s, {"roof_available": False}, FIELDS, "లేదు సార్, మాకు రూఫ్ లేదు")
    assert s.known.get("roof_available") == "False"


def test_a_yes_never_needs_a_negation():
    """Asymmetric on purpose: only the false direction is gated."""
    s = state()
    apply_to_state(s, {"roof_available": True}, FIELDS, FACTORY)
    assert s.known.get("roof_available") == "True"


def test_a_guessed_category_from_garble_is_unaffected_when_positive():
    """`property_type: commercial` came out of "అది ఒక థర్డ్ సెంటర్".

    Right by luck. It is not gated here -- a positive guess costs one wrong
    label, not the call -- but it must not crash the gate either.
    """
    s = state()
    apply_to_state(s, {"property_type": "commercial"}, FIELDS,
                   "అది ఒక థర్డ్ సెంటర్ యాక్చువల్ గా")
    assert s.known.get("property_type") == "commercial"


# --- 7.2b  never hang up on an answer we said we could not hear -------------

def test_no_disqualifying_on_the_turn_after_a_repair():
    """The line that lost the lead."""
    s = state()
    s.misheard_last_turn = True
    apply_to_state(s, {"disqualified": True, "disqualify_reason": "no roof"},
                   FIELDS, TRACTOR)
    assert not s.disqualified


def test_the_grace_lasts_exactly_one_turn():
    """Not a latch. A caller who really is disqualified still gets closed."""
    s = state()
    s.misheard_last_turn = True
    apply_to_state(s, {}, FIELDS, TRACTOR)          # the turn after the repair
    apply_to_state(s, {"disqualified": True}, FIELDS,
                   "మాకు ఇప్పటికే సోలార్ ఉంది")     # the one after that
    assert s.disqualified


def test_disqualifying_normally_still_works():
    s = state()
    apply_to_state(s, {"disqualified": True, "disqualify_reason": "has solar"},
                   FIELDS, "మాకు ఇప్పటికే సోలార్ ఉంది")
    assert s.disqualified


# --- the gate needs evidence to act on --------------------------------------

def test_no_caller_text_means_no_gating():
    """The end-of-call extraction re-reads the whole transcript and passes no
    single utterance. Stripping every negative there would lose real answers."""
    s = state()
    apply_to_state(s, {"roof_available": False}, FIELDS, "")
    assert s.known.get("roof_available") == "False"


# --- the negation vocabulary itself -----------------------------------------

@pytest.mark.parametrize("said,expected", [
    ("లేదు సార్", True),
    ("మాకు రూఫ్ లేదండీ", True),          # agglutinated -- one token
    ("నో నో నో ఐ డోంట్ హావ్", True),     # Sarvam writes English in Telugu script
    ("సొంతం కాదు", True),
    ("అది అద్దె ఇల్లు", True),            # rented is a real "not mine"
    ("అవును ఉంది", False),
    ("టెర్రస్ ఉంది", False),
    ("ఇబ్బంది లేదు", False),             # "no problem" is not an answer of no
    ("నాకు ఇంకో నోట్ ఉంది", False),      # "నో" sits inside "నోట్"
    ("మనోహర్ అండి", False),              # and inside a name
])
def test_negation_vocabulary(said, expected):
    assert is_negative(said) is expected
