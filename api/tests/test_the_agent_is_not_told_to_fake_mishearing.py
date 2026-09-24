"""The state block was telling the model to say it had not heard the caller.

Every checklist turn carried, verbatim:

    ALREADY SAID: '<last question>' -- do not repeat it; say you could not hear.

So whenever the model was about to repeat itself it was instructed to claim a
mishearing -- to a caller it had transcribed perfectly. That is the
"క్షమించండి, సరిగ్గా వినిపించలేదు" the audit found six times across runs 1023
and 1044, and once the model said it, `SAID_NOT_HEARD` set `misheard_last_turn`.

That flag is the second half. It was cleared only by `extractor.py` (which runs
in the simulator alone) and on one move-on path, so after the first "హలో" every
later turn also carried "YOU JUST SAID YOU COULD NOT HEAR THEM" -- in run 1044
from turn 2 onward. It is a ONE-turn fact: the reply it shapes is the reply
straight after, so an ordinary reply getting through clears it.
"""

from api.services.vaani.reply_sanitizer import ReplySanitizer
from api.services.vaani.state import CallState

Q = {"monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
     "location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?"}


def _state():
    s = CallState(required_fields=list(Q), questions=dict(Q))
    s.asked.append(Q["monthly_bill"])
    s.last_user_text = "యాభై వేలు అండి"
    return s


def test_the_block_never_tells_the_model_to_claim_it_could_not_hear():
    block = _state().render()
    assert "say you could not hear" not in block.lower()


def test_it_still_tells_the_model_not_to_repeat_itself():
    assert "do not repeat" in _state().render().lower()


def test_misheard_clears_once_an_ordinary_reply_gets_through():
    from api.services.vaani.brain_processor import ReplyFilter

    s = _state()
    s.misheard_last_turn = True

    class _Inj:
        pass

    inj = _Inj()
    inj.state = s
    f = ReplyFilter.__new__(ReplyFilter)
    f._injector = inj
    f._sanitizer = ReplySanitizer()
    f._spoken = ""
    f._blocked = False
    f._said = []
    f._stale = False
    f._pending_repeat = ""
    f._repair_suppressed = False
    out = f._gate(Q["location"])
    assert out == Q["location"]
    assert s.misheard_last_turn is False, (
        "a one-turn fact left set makes every later turn claim a mishearing")
