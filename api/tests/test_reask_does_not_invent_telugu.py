"""Doc 34: the bookish Telugu is a re-ask artefact, and this is the line.

Measured across MB Solar runs 857-861 (49 agent turns, 39 distinct sentences):
the FIRST ask of a field is 90-100% identical to the question the client wrote,
and every re-ask drifts -- always toward written Telugu.

    run 859 location      ask 1  77%   ask 2  69%   ask 3  62%
    run 861 property_type ask 1  96%   ask 2  75%   ask 3  75%
    run 859 roof          ask 1 100%   ask 2  90%

The third attempt at run 859's location question is the one that produced
"నివసిస్తున్నారు" ("do you reside"). Of the ten worst bookish forms in that
audit, eight appear in NO prompt layer: the model invents them.

It invents them because the state block told it to -- "if you have asked
before, ask it a DIFFERENT way" -- and invented Telugu falls back to the
register the model saw most in training, which is written Telugu.
`speech_register` cannot repair it: it substitutes nouns and deliberately skips
verb morphology, and every one of those forms is a verb.
"""

from __future__ import annotations

from api.services.vaani.state import CallState


def _state() -> CallState:
    s = CallState()
    s.required_fields = ["location"]
    s.questions = {"location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?"}
    return s


def test_the_first_ask_leaves_the_wording_to_the_model():
    s = _state()
    block = s.render()
    assert "WORDING is yours" in block, (
        "the first ask must still follow from what the caller said -- fixing "
        "the wording of every ask is what produced run 817's 'it is 100% "
        "scripted'")


def test_a_reask_must_choose_from_written_wordings_not_invent_one():
    s = _state()
    s.pending_ask = "location"
    s.commit_ask()
    block = s.render()
    assert "do NOT invent new wording" in block
    assert "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?" in block, (
        "a re-ask must be offered the human-written sentence to say")
    assert "ask it a DIFFERENT way" not in block, (
        "this is the clause doc 34 traced the bookish Telugu to")


def test_a_client_may_supply_several_spoken_variants():
    s = _state()
    s.question_variants = {"location": [
        "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?",
        "ఏ ఏరియా అండి?",
        "సిటీలో ఎక్కడ ఉంటున్నారు?"]}
    s.pending_ask = "location"
    s.commit_ask()
    block = s.render()
    for variant in s.question_variants["location"]:
        assert variant in block, (
            "every written variant is offered, so a re-ask can vary without "
            "inventing")


def test_variants_fall_back_to_the_written_question_when_none_are_supplied():
    s = _state()
    assert s.variants_for("location") == [
        "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?"], (
        "no client has written variants yet. Saying the human sentence again "
        "is the safe end of the trade -- and the answer budget now means the "
        "caller hears it at most twice")
    assert s.variants_for("nonexistent_field") == []
