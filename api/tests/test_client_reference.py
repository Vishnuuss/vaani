"""Layer 3 reference material, charged on the turn it is needed.

Driven with MB Solar's real rows, because the point of the module is the cost
of MB Solar's real prompt: Rs 3.56 a call against Investment's Rs 1.40 on the
same build, a fifth of the gap being 7,220 chars of FAQ re-read every turn.
"""

from __future__ import annotations

import pytest

from api.services.vaani import client_reference as cref

PROMPT = """# MB Solar Hub — Priya

## THE SIX QUESTIONS — this is the whole job

Ask them in order.

## Compliance — absolute, no exceptions

Never promise a subsidy amount.

## Reference — the rest of this document

Never read this out.

**Is there a subsidy? / How much subsidy?**
TRIGGER: సబ్సిడీ|subsidy|స్కీమ్|scheme
"PM Surya Ghar స్కీమ్ లో మొదటి two kW కి kW కి thirty thousand rupees."
Never say the money is approved for THIS caller.

**Do I need a battery?**
TRIGGER: బ్యాటరీ|battery|బ్యాకప్
"అవసరం లేదు అండి. చాలా వరకు on-grid సిస్టమ్స్."

**Anything else at all**
"అది నేను కనుక్కుని చెప్తాను అండి."
"""


def test_the_operational_half_is_what_the_agent_carries():
    operational, reference = cref.split(PROMPT)
    assert "THE SIX QUESTIONS" in operational
    assert "Compliance" in operational
    assert "సబ్సిడీ" not in operational, "reference leaked into every turn"
    assert reference.startswith("## Reference")


def test_a_prompt_with_no_reference_heading_is_unchanged():
    """An un-migrated agent must behave exactly as it does today."""
    plain = "# Some other agent\n\n## Questions\n\nAsk them."
    operational, reference = cref.split(plain)
    assert operational == plain
    assert reference == ""


def test_the_split_loses_nothing():
    operational, reference = cref.split(PROMPT)
    for fragment in ["THE SIX QUESTIONS", "Compliance", "సబ్సిడీ",
                     "బ్యాటరీ", "కనుక్కుని చెప్తాను"]:
        assert fragment in operational + reference, f"{fragment} was deleted"


def test_rows_parse_with_their_triggers():
    rows = cref.parse(cref.split(PROMPT)[1])
    titles = [r.title for r in rows]
    assert "Is there a subsidy? / How much subsidy?" in titles
    assert "Do I need a battery?" in titles
    subsidy = next(r for r in rows if r.title.startswith("Is there a subsidy"))
    assert subsidy.pattern is not None
    assert "TRIGGER:" not in subsidy.body, "the trigger line is not spoken material"
    assert "Never say the money is approved" in subsidy.body, (
        "the caveat under the answer must survive -- it is the compliance half")


@pytest.mark.parametrize("said", ["సబ్సిడీ ఎంత వస్తుంది?", "subsidy ఎంత?",
                                  "ఆ స్కీమ్ గురించి చెప్పండి"])
def test_the_caller_asking_pulls_the_row(said):
    rows = cref.parse(cref.split(PROMPT)[1])
    out = cref.lookup(rows, said)
    assert out and "PM Surya Ghar" in out[0]


@pytest.mark.parametrize("said", ["హైదరాబాద్.", "కమర్షియల్ అండి", "ఆ", ""])
def test_an_ordinary_answer_pulls_nothing(said):
    """The whole saving: most turns cost zero extra tokens."""
    rows = cref.parse(cref.split(PROMPT)[1])
    assert cref.lookup(rows, said) == []


def test_a_row_without_a_trigger_never_fires():
    """Matching on the English heading would fire it on the wrong question."""
    rows = cref.parse(cref.split(PROMPT)[1])
    catch_all = next(r for r in rows if r.title == "Anything else at all")
    assert catch_all.pattern is None
    assert cref.lookup(rows, "anything else at all") == []


def test_at_most_two_rows_and_never_the_same_one_twice():
    rows = cref.parse(cref.split(PROMPT)[1])
    both = cref.lookup(rows, "సబ్సిడీ మరియు బ్యాటరీ గురించి చెప్పండి")
    assert len(both) <= cref.MAX_ROWS
    again = cref.lookup(rows, "సబ్సిడీ ఎంత?",
                        exclude={"Is there a subsidy? / How much subsidy?"})
    assert not any("PM Surya Ghar" in l for l in again)


def test_a_broken_trigger_regex_does_not_take_the_call_down():
    bad = PROMPT.replace("TRIGGER: బ్యాటరీ|battery|బ్యాకప్", "TRIGGER: (unclosed[")
    rows = cref.parse(cref.split(bad)[1])
    battery = next(r for r in rows if r.title == "Do I need a battery?")
    assert battery.pattern is None
    assert cref.lookup(rows, "బ్యాటరీ కావాలా?") == []


def test_an_over_long_row_is_skipped_rather_than_read_out():
    huge = PROMPT.replace('"అవసరం లేదు అండి. చాలా వరకు on-grid సిస్టమ్స్."',
                          '"' + ("పొడవు " * 400) + '"')
    rows = cref.parse(cref.split(huge)[1])
    assert cref.lookup(rows, "బ్యాటరీ కావాలా?") == []
