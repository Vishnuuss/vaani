"""The just-in-time coach: it must fire on the right turns and only those.

A `COACH` line lands in the trailing state block, which `state.render` documents
as the most authoritative position in the whole context -- the model obeys it
over the prose above. That makes a wrong line worse than no line, so these tests
weight precision over recall, the same way `triage`'s do.
"""

from __future__ import annotations

import pytest

from api.services.vaani import coach as C
from api.services.vaani.state import CallState


# --- every cue fires on the utterance it was written for ---------------------
#
# Written as the caller's real words, not as the regex read backwards. Several
# of these were MISSES on the first pass -- "మా దగ్గర already ఉంది" (an English
# adverb on a Telugu verb), "మీ నంబర్ ఎక్కడిది?" (మీ, not నా) and a bare
# "ఎంత అవుతుంది?" all walked straight past the first version of the catalogue.
FIRES = [
    ("నెల బిల్లు లక్ష దాటుతుంది సార్", "bill_shock"),
    ("ఏంటి ఈ చెత్త కాల్, చిరాకు వస్తుంది", "angry"),
    ("నేను డ్రైవింగ్‌లో ఉన్నాను, టైమ్ లేదు", "hurry"),
    ("నాకు నమ్మకం లేదు", "suspicious"),
    ("ఏంటి catch?", "suspicious"),
    ("అర్థం కాలేదు, ఏం చెప్పాలి?", "confused"),
    ("మా నాన్నగారు హాస్పిటల్‌లో ఉన్నారు", "sad"),
    ("చాలా ఖరీదు అనిపిస్తుంది", "too_expensive"),
    ("నా దగ్గర డబ్బు లేదు", "no_money"),
    ("ఆసక్తి లేదు అండి", "not_interested"),
    ("నేను ఆలోచించి చెప్తాను", "think_about_it"),
    ("తర్వాత కాల్ చేయండి", "call_later"),
    ("ఇప్పుడు కుదరదు", "call_later"),
    ("వాట్సప్ చేయండి", "whatsapp"),
    ("మా ఆవిడని అడగాలి", "ask_family"),
    ("మా దగ్గర already ఉంది", "already_have"),
    ("వేరే కంపెనీ కూడా కాల్ చేసింది", "competitor"),
    ("మీరు ఎక్కడి నుంచి మాట్లాడుతున్నారు?", "who_are_you"),
    ("మీ నంబర్ ఎక్కడిది?", "who_are_you"),
    ("రోజూ చాలా మంది కాల్ చేస్తున్నారు", "too_many_calls"),
    ("ఇది ఫ్రీనా?", "is_it_free"),
    ("ఇల్లు అద్దెకు ఉంటున్నాను", "rented"),
    ("వారంటీ ఎన్నేళ్ళు?", "guarantee"),
    ("ఒకసారి వచ్చి చూడండి", "come_and_see"),
    ("ఇది ఎలా పని చేస్తుంది?", "how_does_it_work"),
    ("ఎంత అవుతుంది?", "price_question"),
]


@pytest.mark.parametrize("text,expected", FIRES)
def test_the_cue_fires_on_its_own_utterance(text, expected):
    assert expected in [c.name for c in C.cues_for(text)], (
        f"{expected} did not fire on {text!r}")


# --- and nothing fires on an ordinary answer ---------------------------------
#
# The expensive failure mode. A caller who says "సరే" has said nothing at all,
# and a coaching line there would push the agent into handling an objection
# that was never raised.
QUIET = [
    "సరే",
    "హా",
    "ఆ అవును",
    "నా పేరు రవి",
    "విజయవాడ",
    "రేపు ఉదయం సరిపోతుంది",
    "కాంక్రీట్ రూఫ్ ఉంది",
    "సొంత ఇల్లే",
]


@pytest.mark.parametrize("text", QUIET)
def test_an_ordinary_answer_gets_no_coaching(text):
    assert C.coach(text) == [], f"coached an ordinary answer: {text!r}"


def test_nothing_at_all_on_empty_input():
    assert C.coach("") == []
    assert C.coach(None) == []


# --- the budget --------------------------------------------------------------
#
# This block is the UNCACHED tail, re-read and re-billed on every single turn,
# and prompt volume is latency on a reasoning model. The whole point of moving
# the catalogue out of the layers is lost if the injection is unbounded.


def test_at_most_two_lines_however_many_cues_match():
    # Angry AND in a hurry AND the price AND the family, all in one breath.
    piled = ("చిరాకు వస్తుంది, టైమ్ లేదు, చాలా ఖరీదు, మా ఆవిడని అడగాలి")
    assert len(C.cues_for(piled)) > 2
    assert len(C.coach(piled)) == C.MAX_LINES


def test_every_line_in_the_catalogue_fits_the_budget():
    for cue in C.CUES:
        line = f"COACH ({cue.name}): {cue.say}"
        assert len(line) <= C.MAX_CHARS, f"{cue.name} is {len(line)} chars"


def test_emotion_outranks_the_objection_underneath_it():
    """How they said it decides the SHAPE of the reply; content in the wrong
    shape is worse than no content."""
    both = "చిరాకు వస్తుంది, చాలా ఖరీదు కూడా"
    assert C.cues_for(both)[0].name == "angry"


# --- one rebuttal, never two -------------------------------------------------


def test_a_cue_already_spent_is_not_injected_again():
    spent = {"too_expensive"}
    assert C.coach("చాలా ఖరీదు", spent) == []


def test_a_different_cue_still_gets_through():
    spent = {"too_expensive"}
    out = C.coach("చాలా ఖరీదు, పైగా మా ఆవిడని అడగాలి", spent)
    assert len(out) == 1 and "ask_family" in out[0]


# --- and it actually reaches the prompt --------------------------------------


def _state() -> CallState:
    return CallState(required_fields=["monthly_bill"],
                     questions={"monthly_bill": "మీ బిల్లు ఎంత?"})


def test_the_state_block_carries_the_coaching():
    state = _state()
    state.last_user_text = "ఒకసారి వచ్చి చూడండి"
    assert "COACH (come_and_see)" in state.render()


def test_rendering_twice_does_not_repeat_the_same_coaching():
    """`render` is called per turn and the state block is rebuilt each time.
    A cue spent on turn four must not come back on turn five."""
    state = _state()
    state.last_user_text = "చాలా ఖరీదు"
    assert "COACH (too_expensive)" in state.render()
    assert "COACH (too_expensive)" not in state.render()


def test_an_ordinary_turn_adds_nothing_to_the_block():
    state = _state()
    state.last_user_text = "సరే"
    assert "COACH" not in state.render()


def test_the_catalogue_carries_no_industry_vocabulary():
    """Layers 1, 2 and 4 are inherited unchanged by every client, and this
    catalogue is read on every call for every client -- so it lives under the
    same rule. Layer 3 is the only place a trade word belongs."""
    industry = ("solar", "సోలార్", "panel", "ప్యానెల్", "roof", "రూఫ్",
                "subsidy", "సబ్సిడీ", "kw", "insurance", "loan")
    for cue in C.CUES:
        low = cue.say.lower()
        for word in industry:
            assert word not in low, f"{cue.name} names {word!r}"


# --- SEGMENT: who the caller is, 4 Sep ---------------------------------------
#
# "anything i ask from website it should answer perfectly ... not simply saying
# like brain roting ... it should explain in simple way"
#
# The first attempt wrote a client's whole service catalogue into the node
# prompt: 9,811 -> 14,904 characters, and the eval's verbatim-repetition
# failures went from 1 to 8. So the part that GENERALISES -- what kind of place
# the caller is ringing about, which is the same set in every industry -- lives
# here, one row chosen by the caller's own words. What that segment actually
# buys stays in Layer 3, per client, because this file is read on every call
# for every client.


@pytest.mark.parametrize("said,cue", [
    ("మా సొసైటీలో లిఫ్ట్ కి కరెంట్ ఎక్కువ అవుతుంది", "segment_society"),
    ("we are a group housing society", "segment_society"),
    ("నేను అపార్ట్మెంట్ లో ఉంటాను", "segment_apartment"),
    ("మాది warehouse ఉంది", "segment_warehouse"),
    ("హాస్పిటల్ కి కావాలి", "segment_institution"),
    ("our school needs solar", "segment_institution"),
    ("మాకు factory ఉంది", "segment_industry"),
    ("మా ఆఫీసుకి కావాలి", "segment_office"),
    ("ఖాళీ ల్యాండ్ ఉంది", "segment_land"),
    ("job ఉందా మీ దగ్గర", "careers"),
    ("వెండర్స్ ని ఎలా verify చేస్తారు", "vendor_check"),
    ("ఎప్పుడు call చేస్తారు", "response_time"),
])
def test_the_segment_cues_fire_on_the_callers_own_words(said, cue):
    assert cue in {c.name for c in C.cues_for(said)}


def test_a_segment_cue_never_outranks_anger():
    """A man who is angry about his society's bill gets the anger row. The
    fact is useless in the wrong shape -- that is the rule TONE already sets."""
    lines = C.coach("మా సొసైటీ గురించి అడిగితే చిరాకు వస్తుంది నాకు")
    assert lines and "angry" in lines[0]


def test_every_cue_fits_the_block():
    for cue in C.CUES:
        assert len(f"COACH ({cue.name}): {cue.say}") <= C.MAX_CHARS, cue.name


def test_an_ordinary_answer_earns_no_segment_row():
    assert C.coach("నా పేరు రమేష్") == []
