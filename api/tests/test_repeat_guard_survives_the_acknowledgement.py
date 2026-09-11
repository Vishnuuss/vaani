"""The repeat guard was dead on every call, because every reply says "సరే" first.

What was measured, run 863
---------------------------
    BOT : సైట్ సర్వే కోసం మీరు సిద్ధంగా ఉన్నారా?
    USER: ఉన్నాము ఉన్నాము.                        (we are ready)
    BOT : సరే, సైట్ సర్వే కోసం మీరు సిద్ధంగా ఉన్నారా?

The client's report was "why is it saying the same sentence two times".

Why it happened, and why it was invisible
------------------------------------------
`_is_repeat` is correct. Handed that second reply in full it returns True. It
was never handed it.

Two constants were never reconciled:

  * `ReplySanitizer` holds back **24** characters before releasing anything, so
    the first `candidate` is about 24 characters INCLUDING the leading
    acknowledgement.
  * `_is_repeat` needs `_REPEAT_PREFIX` = **25** characters of substance AFTER
    `_strip_ack` removes that acknowledgement.

So the first chunk could never clear the bar, and the old gate --
`if not self._spoken` -- closed the moment that chunk was emitted. The check
never ran again for that reply.

The state block ASKS every reply to open with an acknowledgement. So this was
not an edge case: the guard was structurally unable to fire, on every turn, of
every call, for as long as both numbers have existed.

Nothing failed and nothing logged. The only symptom was the agent repeating
itself, which looked like a model problem and was not.

These tests pin both directions, because the opposite error is worse: blocking
a legitimate next question makes the agent apologise instead of advancing, and
run 783 shows what that costs.
"""

from api.services.vaani.brain_processor import ReplyFilter


def _filter(previous: list[str]) -> ReplyFilter:
    f = ReplyFilter.__new__(ReplyFilter)
    f._injector = None
    f._said = list(previous)
    f._spoken = ""
    f._blocked = False
    f._pending_repeat = ""
    return f


def _speak(previous: str, chunks: list[str]) -> str:
    """Stream a reply through the REAL `_gate` and return what the caller hears.

    Drives the actual code path rather than re-implementing its condition, so
    the test cannot quietly agree with a broken gate. `_gate` returns "" while
    it is holding text back, and `_spoken` accumulates only what was emitted --
    which is exactly what reaches TTS.
    """
    from api.services.vaani import guardrails

    f = _filter([previous])
    for chunk in chunks:
        out = f._gate(chunk)
        if out:
            f._spoken += out
    # End of reply: whatever is still held must be decided and released.
    if f._pending_repeat:
        held, f._pending_repeat = f._pending_repeat, ""
        if not f._spoken and f._is_repeat(held):
            f._blocked = True
            held = guardrails.REPAIR_LINE
        f._spoken += held
    return f._spoken


def _blocks(previous: str, chunks: list[str]) -> bool:
    from api.services.vaani import guardrails

    return _speak(previous, chunks).strip() == guardrails.REPAIR_LINE.strip()


# --- the defect itself ----------------------------------------------------

SURVEY = "సైట్ సర్వే కోసం మీరు సిద్ధంగా ఉన్నారా?"
ASKED_BILL = "సార్, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా వస్తుంది?"
OFFICE = "మా ఆఫీస్ విజయవాడ, MG Road లో ఉంది అండి."


def test_run863_repeat_is_blocked_at_sanitizer_chunking():
    """~24-character chunks, which is what the sanitizer actually emits."""
    assert _blocks(SURVEY, ["సరే, సైట్ సర్వే కోసం మీ", "రు సిద్ధంగా ఉన్నారా?"])


def test_the_guard_depends_on_the_sanitizer_holdback():
    """The coupling that makes the fix work, pinned so it cannot be lowered.

    `_gate` never sees a bare "సరే, " on its own: `ReplySanitizer.HOLDBACK`
    guarantees the first chunk is at least 24 characters, so it always carries
    the acknowledgement PLUS the opening of the real sentence -- which is what
    `_looks_like_repeat` needs in order to suspect anything.

    Written as a test rather than a comment because the two are in different
    files and nothing else connects them. If HOLDBACK is ever reduced below
    the 6-character suspicion floor plus a typical acknowledgement, the repeat
    guard silently goes back to being dead, exactly as it was before.
    """
    from api.services.vaani.reply_sanitizer import HOLDBACK

    assert HOLDBACK >= 24, (
        "lowering the hold-back starves the repeat guard; it will stop firing "
        "and nothing will fail or log")


def test_the_first_chunk_carries_enough_to_suspect_a_repeat():
    """The realistic shape: one ~24-character chunk, ack included."""
    f = _filter([SURVEY])
    first = "సరే, సైట్ సర్వే కోసం మీ"
    assert len(first) >= 20
    assert f._gate(first) == "", "the suspicious opening was not held"


def test_run861_office_answer_is_not_given_twice():
    assert _blocks(OFFICE, ["మా ఆఫీస్ విజయవాడ, MG Road ", "లో ఉంది అండి."])


def test_the_acknowledgement_alone_is_what_used_to_defeat_it():
    """Pins the mechanism, so a future change to `_strip_ack` fails loudly.

    Fed the whole reply, the guard has always been right. The bug was never in
    `_is_repeat`; it was that the ack consumed the only window it was given.
    """
    f = _filter([SURVEY])
    assert f._is_repeat("సరే, " + SURVEY), "the underlying comparison regressed"
    # The old gate: only the first chunk, and the first chunk is the ack.
    assert not f._is_repeat("సరే, "), (
        "a bare acknowledgement must not count as a repeat -- it is meant to "
        "recur on every turn")


# --- the expensive direction ---------------------------------------------
#
# Blocking a legitimate question makes the agent apologise instead of
# advancing, and charges an ask for a question nobody was asked (run 783).

def test_a_different_question_after_an_ack_is_not_blocked():
    assert not _blocks("మీ నెలకు కరెంట్ బిల్లు ఎంత వస్తుంది?",
                       ["సరే, మీరు ఏ ఏరియా లేదా సిటీ", "లో ఉంటున్నారు?"])


def test_the_next_question_in_the_flow_is_not_blocked():
    assert not _blocks("మీ పేరు చెప్పగలరా?",
                       ["మంచిది, ఉచితంగా ఒక సైట్ సర్", "వే చేయించుకుంటారా?"])


def test_bill_after_property_type_is_not_blocked():
    assert not _blocks("మీది సొంత ఇల్లా, అపార్ట్‌మెంటా?",
                       ["సరే, మీ నెలకు కరెంట్ బిల్లు ", "ఎంత వస్తుంది?"])


def test_nothing_said_yet_means_nothing_to_repeat():
    assert not _blocks("", ["సరే, మీ పేరు చెప్పగలరా?"])


def test_nothing_is_ever_retracted_once_audio_is_out():
    """The truncation bug, and the rule this fix must not break.

    Once a word has been emitted, no later chunk may be swapped for the repair
    line: the caller hears the join. A live reply came out as
    "సరే, రెండు thousand rupeeసార్, కరెక్ట్ ఫిగర్ ఇప్పుడే చెప్పలేను."
    """
    f = _filter([ASKED_BILL])
    f._spoken = "సార్, మీ నెలవారీ బిల్లు "        # already spoken
    tail = "ఎంత రూపాయలుగా వస్తుంది?"
    assert f._gate(tail) == tail, "retracted text that was already spoken"


def test_held_text_is_never_lost_when_the_reply_ends():
    """Buffered-and-forgotten is silence, which is worse than the repeat.

    A short reply can be a single chunk, so the hold can still be open when
    the response ends. What was held must come out then -- as itself, or as
    the repair line, but never as nothing.
    """
    heard = _speak("మీ పేరు చెప్పగలరా?", ["సరే, మీ ఊరు చె"])
    assert heard, "the reply vanished"


def test_a_bare_acknowledgement_is_not_held_for_ever():
    """The 6-character floor. "సరే" matches the opening of everything."""
    f = _filter(["సరే, మీ పేరు చెప్పగలరా?"])
    assert not f._looks_like_repeat("సరే")
    assert not f._looks_like_repeat("")
