"""A spoken reply must never begin with whitespace.

Run 792 shipped one that did, on every single turn, and it took a live call to
notice. Semantic turn completion puts a marker before the MODE line:

    ✓ MODE: ASK

    మంచిది సార్, ...

pipecat's mixin strips the marker and one space, but only from the CHUNK THE
MARKER ARRIVED IN. When the stream delivers "✓" as a token of its own there is
nothing left to strip from it, so the next chunk arrives as " MODE: ASK" with
its leading space intact. `ReplySanitizer.MODE_RE` then removes the MODE line
and one newline and leaves a space and a newline at the front of the reply.

Every guard between there and the saved transcript tests truthiness, and
whitespace is truthy, so it was pushed to TTS and written to the transcript.

The narrow fix is to widen MODE_RE. The fix here is the class of bug instead:
the front of a reply is not a place where whitespace can carry meaning, so it is
dropped until something real has been said -- whatever produced it, and whatever
is added upstream later.
"""

from __future__ import annotations

import pytest

from api.services.vaani.reply_sanitizer import ReplySanitizer


def _stream(chunks):
    s = ReplySanitizer()
    return "".join(s.feed(c) for c in chunks) + s.finish()


def test_a_lone_marker_token_does_not_leave_a_gap():
    """Run 792 exactly: the marker arrives as its own token."""
    out = _stream(["✓", " MODE: ASK\n\n", "మంచిది సార్, మీ బిల్లు ఎంత?"])
    assert not out[:1].isspace(), repr(out[:12])
    assert out.startswith("మంచిది")


def test_the_mode_line_alone_leaves_no_gap():
    out = _stream(["MODE: ASK\n\n", "మీ పేరు చెప్పగలరా?"])
    assert not out[:1].isspace(), repr(out[:12])


def test_leading_whitespace_of_any_shape_is_dropped():
    for lead in (" ", "\n", "\n\n", " \n", "\t ", "   \n  "):
        out = _stream([lead, "సరే అండి."])
        assert out.startswith("సరే"), repr((lead, out[:10]))


def test_whitespace_INSIDE_the_reply_is_untouched():
    """The rule is about the front only; it must not reflow real speech."""
    out = _stream(["సరే అండి,", " మీ బిల్లు ఎంత?"])
    assert "సరే అండి, మీ బిల్లు ఎంత?" == out.strip()
    assert " మీ" in out


def test_a_reply_that_is_only_whitespace_stays_empty():
    """Nothing real was said, so nothing should be spoken or stored."""
    assert _stream(["✓", " MODE: ASK\n", "\n"]).strip() == ""


def test_the_mode_is_still_read_off_the_stripped_line():
    """MODE is load-bearing -- MODE: END is the only thing that hangs up."""
    s = ReplySanitizer()
    for c in ["✓", " MODE: END\n\n", "థాంక్యూ సార్."]:
        s.feed(c)
    s.finish()
    assert s.mode == "END"
