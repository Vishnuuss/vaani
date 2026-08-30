"""Run 324 -- the call where the agent would not listen.

WR-TEL-OUT-08276281, 30 August, 64 seconds, and it ends with four of six fields
null. The client's summary of it: "it is not listening, it is just doing its
work fastly... it has a time bound; if I answer in that time bound it replies,
if past that time it moves to the next question or interrupts me."

The transcript, with the seconds:

    16.52  USER  అదే ఇప్పుడైతే ఏం రాదు కానీ నెక్స్ట్ 2010.
    17.99  USER  3 లాక్స్ ఎట్లా వస్తాది?              <- he asks a question
    18.00  BOT   మంచిది సార్, మీరు                    <- 10 ms later
    ...
    38.20  BOT   సరే, మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?
    40.58  USER  నేను చెప్తే వినండి.                  <- LISTEN TO WHAT I SAY
    43.02  BOT   సరే, మీరు ఏ ఏరియా లేదా సిటీలో ఉంటారు?  <- asks it again
    45.68  USER  నేను చెప్పేది మాకు                   <- cut off mid-sentence
    49.48  BOT   మంచిది సార్, మీది సొంత ఇల్లా ...       <- moves on anyway
    53.35  USER  కాదు.
    57.52  BOT   సార్, మీరు రూఫ్ లేదా టెర్రస్ కలిగి లేకపోతే మేము కొనసాగించలేము.

`location` saved as null. `roof_available` saved as null -- the roof question
was never asked, and the call was ended on it.
"""

import pytest

from api.services.vaani import triage
from api.services.vaani.completeness import sounds_unfinished
from api.services.vaani.speech_register import spoken
from api.services.vaani.state import CallState
from api.services.vaani.telugu_turn import (
    CUTOFFS_BEFORE_ADAPTING,
    TeluguTurnAnalyzer,
    TeluguTurnParams,
)

FIELDS = ["monthly_bill", "location", "property_type", "roof_available"]


def state():
    return CallState(required_fields=FIELDS,
                     questions={f: f"[{f}]?" for f in FIELDS})


def asked_once(field="monthly_bill"):
    """A state where exactly one question has been put to the caller."""
    s = state()
    s.render()
    s.commit_ask()
    assert s.ask_counts.get(field) == 1, s.ask_counts
    return s


# --- 1. a sentence that is still running --------------------------------------

@pytest.mark.parametrize("said", [
    "మాది వచ్చేసి మామూలుగా ఇప్పుడైతే",     # run 324, the bill question
    "నాదైతే",                              # the same suffix, bare
    "దేని గురించి",                         # a postposition with no head
    "మా ఇంటి కోసం",
])
def test_a_sentence_that_cannot_end_there_holds_the_turn(said):
    """Each of these is a grammatical CLASS, not a phrase somebody happened to
    say. A postposition governs a head that comes next; -అయితే sets up a
    contrast the rest of the sentence has to supply."""
    assert sounds_unfinished(said), said


@pytest.mark.parametrize("said", [
    "హైదరాబాద్", "ఇల్లు", "కైలాష్", "ఆ ఉంది", "అవును సార్",
    "యాభై వేలు వస్తుంది అండి",
])
def test_a_finished_answer_is_not_held(said):
    """The cost of a false positive is 0.45 s on every turn it fires. Measured
    at 8.02% across 2,754 real turn ends, against 7.73% before these rules."""
    assert not sounds_unfinished(said), said


def test_the_agglutinated_form_is_matched_not_just_the_free_one():
    """Telugu agglutinates: ఇప్పుడు + అయితే surfaces as ఇప్పుడైతే, and the
    literal string "అయితే" is no longer anywhere in the word. Checking only the
    free form is how this rule matched nothing on the utterance it was written
    for."""
    assert sounds_unfinished("ఇప్పుడైతే")
    assert "అయితే" not in "ఇప్పుడైతే"


# --- 2. the caller asking to be heard -----------------------------------------

@pytest.mark.parametrize("said", [
    "నేను చెప్తే వినండి.",
    "చెప్పండి వినండి మీరు.",
    "నేను చెప్పేది వినండి",
    "ఒక్క నిమిషం ఆగండి",
    "let me finish",
    "listen to me",
])
def test_asking_to_be_heard_stops_the_agent_asking(said):
    s = asked_once()
    s.last_user_text = said
    triage.apply(s, said)
    block = s.render()
    assert "HE ASKED YOU TO LISTEN" in block, block
    assert "Ask NOTHING this turn" in block


def test_asking_to_be_heard_does_not_end_the_call():
    """It is not a refusal and not a deferral. Both of those mean stop; this
    one means wait."""
    s = asked_once()
    triage.apply(s, "నేను చెప్తే వినండి.")
    assert not s.must_end
    assert not s.disqualified


def test_the_floor_is_given_for_exactly_one_turn():
    s = asked_once()
    s.last_user_text = "నేను చెప్తే వినండి."
    triage.apply(s, "నేను చెప్తే వినండి.")
    s.render()
    s.last_user_text = "హైదరాబాద్"
    assert "HE ASKED YOU TO LISTEN" not in s.render()


# --- 3. an ask the caller was talked over is refunded -------------------------

def test_being_interrupted_does_not_spend_a_lead_question():
    """The half that actually fixes run 324. `location` was asked twice -- once
    normally, once over the top of him -- so the two-ask budget was spent and
    the field dropped off the checklist for the rest of the call."""
    s = state()
    for _ in range(2):
        s.render()
        s.commit_ask()
    assert "monthly_bill" not in s.still_need, "the budget is spent"
    triage.apply(s, "నేను చెప్తే వినండి.")
    assert "monthly_bill" in s.still_need, "he asked to be heard; ask him again"


def test_a_sentence_we_cut_in_half_does_not_spend_an_ask_either():
    s = state()
    for _ in range(2):
        s.render()
        s.commit_ask()
    triage.apply(s, "మాది వచ్చేసి మామూలుగా ఇప్పుడైతే")
    assert "monthly_bill" in s.still_need


def test_a_real_answer_still_spends_the_ask():
    """The budget exists to stop run 218's interrogation. Refunds must not
    quietly rebuild it."""
    s = state()
    for _ in range(2):
        s.render()
        s.commit_ask()
    triage.apply(s, "యాభై వేలు")
    assert "monthly_bill" not in s.still_need


def test_one_interruption_refunds_at_most_one_ask():
    """Otherwise a caller who is cut off repeatedly earns the field an
    unbounded budget, which is run 218 rebuilt out of refunds."""
    s = state()
    for _ in range(2):
        s.render()
        s.commit_ask()
    before = s.ask_counts.get("monthly_bill", 0)
    triage.apply(s, "నేను చెప్తే వినండి.")
    triage.apply(s, "నేను చెప్తే వినండి.")
    assert s.ask_counts.get("monthly_bill", 0) == before - 1


# --- 4. the agent may not hang up on a fact it never asked for ---------------

def test_a_disqualifier_needs_a_fact_behind_it():
    """Run 324 ended the call saying "if you don't have a roof we cannot
    continue" while roof_available was null and had never been asked."""
    from api.services.vaani.extractor import apply_to_state
    s = state()
    s.learn("monthly_bill", "2010")
    apply_to_state(s, {"disqualified": True, "disqualify_reason": "no roof"},
                   FIELDS, user_text="కాదు.")
    assert not s.disqualified, (
        "nothing in that turn said anything about a roof")


def test_a_disqualifier_backed_by_the_same_turn_still_fires():
    """The gate must not make the agent undisqualifiable."""
    from api.services.vaani.extractor import apply_to_state
    s = state()
    apply_to_state(s, {"roof_available": "no", "disqualified": True,
                       "disqualify_reason": "no roof"},
                   FIELDS, user_text="రూఫ్ లేదు")
    assert s.disqualified


# --- 5. the model that reads this caller badly gets more patient --------------

def test_a_caller_we_never_interrupt_is_not_slowed_down():
    a = TeluguTurnAnalyzer(sample_rate=8000, params=TeluguTurnParams())
    assert not a.interrupting
    assert a._band() == a.params.unsure_band == 0.95


def test_two_cutoffs_make_the_floor_unconditional_for_that_caller():
    """Measured on the 1,393 labelled clips: the median wait for a caller in
    this state goes 0.057 -> 0.300 s, and stays BELOW the old flat 0.2 s on the
    mean. Nobody else pays it."""
    a = TeluguTurnAnalyzer(sample_rate=8000, params=TeluguTurnParams())
    a._cutoffs = CUTOFFS_BEFORE_ADAPTING
    assert a.interrupting
    assert a._band() == 1.0


def test_the_patience_is_per_call():
    """One impatient caller must not make the next one slower."""
    a = TeluguTurnAnalyzer(sample_rate=8000, params=TeluguTurnParams())
    a._cutoffs = 5
    b = TeluguTurnAnalyzer(sample_rate=8000, params=TeluguTurnParams())
    assert a.interrupting and not b.interrupting


# --- 6. arithmetic is written, not spoken ------------------------------------

def test_the_agent_does_not_read_an_equation_out_loud():
    said = spoken("2 kW + + 1 kW = = 3 kW మొత్తం 78,000 రూపాయల వరకు")
    assert "+" not in said and "=" not in said, said
    assert "2 kW plus 1 kW అంటే 3 kW" in said, said


def test_ordinary_speech_is_untouched_by_the_arithmetic_rule():
    for said in ["మొత్తం 78,000 రూపాయలు", "రేపు ఉదయం ten o'clock",
                 "రేపు ఉదయం పది గంటలకు"]:
        assert spoken(said) == said, said
