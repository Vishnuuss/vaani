"""The lines the CODE puts in the caller's ear, not the model.

Three of them exist, and two were written in a register the agent never speaks.
Everything the model says is `అండి`; `guardrails.SAFE_CLOSE` and
`SAFE_FALLBACK` open with `సార్`. On a real call that is an audible break --
a different person answering the last sentence.

Worse, `SAFE_CLOSE` is the LAST thing every closed call hears, and it said:

    "సరే సార్, మీ టైమ్ ఇచ్చినందుకు థాంక్యూ. మంచి రోజు సార్."

`మంచి రోజు` is a word-for-word calque of "good day". Telugu speakers do not
say it on the phone. `end_call_bridge.py` already records
"ధన్యవాదాలు, మంచి రోజు!" as a known past defect, and this line still said it.

And `సార్` is not neutral: roughly half the callers on a solar list are women,
for whom it is simply the wrong word. `అండి` is respectful and carries no
gender, which is exactly why the rest of the agent uses it.
"""

from api.services.vaani import guardrails

CANNED = {
    "SAFE_CLOSE": guardrails.SAFE_CLOSE,
    "SAFE_FALLBACK": guardrails.SAFE_FALLBACK,
    "REPAIR_LINE": guardrails.REPAIR_LINE,
    "OPEN_LINE": guardrails.OPEN_LINE,
}


def test_no_canned_line_says_good_day():
    """A calque nobody says, and already on this project's defect list."""
    assert "మంచి రోజు" not in guardrails.SAFE_CLOSE


def test_no_canned_line_assumes_the_caller_is_a_man():
    """Half a solar call list is women. `అండి` is respectful and genderless."""
    offenders = {name: line for name, line in CANNED.items() if "సార్" in line}
    assert not offenders, (
        f"these address the caller as సార్: {sorted(offenders)}")


def test_every_canned_line_keeps_the_agents_register():
    """The whole agent speaks `అండి`. A canned line that does not is audibly a
    different speaker taking over."""
    missing = [name for name, line in CANNED.items() if "అండి" not in line]
    assert not missing, f"these break the అండి register: {missing}"


def test_the_closing_line_does_not_ask_a_question():
    """It is used when the call MUST close and the draft kept interrogating.
    Ending on a question invites an answer and reopens the call."""
    assert "?" not in guardrails.SAFE_CLOSE


def test_the_closing_line_is_short_enough_to_say_in_one_breath():
    assert len(guardrails.SAFE_CLOSE) <= 70, guardrails.SAFE_CLOSE


def test_the_fallback_still_promises_a_follow_up():
    """It exists to decline a number gracefully, so it must still say someone
    will come back with the real one -- otherwise it is just a refusal."""
    assert "టీమ్" in guardrails.SAFE_FALLBACK
