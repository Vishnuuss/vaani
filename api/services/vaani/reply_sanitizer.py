"""Strip control tokens and invented dialogue out of a streaming reply.

Why this is not just the old header-stripper
--------------------------------------------
`ReplyFilter` used to partition the buffer on its first newline and drop the head
if it began with `MODE:`. That is a one-shot header stripper: after the first
newline every later line passed through untouched. Run 12 exploited exactly that
hole and the caller heard the control token out loud:

    ...need agreement.MODE: CLOSE

Note there is no newline before `MODE:` there, which is also why no stop sequence
can catch it -- `reply_bounds` explains that limit. The marker has to be found
wherever it lands, so this scans the text rather than only its first line.

Three defects, three rules
--------------------------
1. `MODE:` anywhere is removed, not only in first position, and `MODE : END`
   (space before the colon) counts -- the old `startswith("MODE:")` test missed
   it silently.
2. A second speaker's turn label truncates the rest of the reply. Once the model
   writes `CUSTOMER:` it has stopped answering and started scripting, and
   everything after it is invention.

3. The turn ends at its first question mark. This is the rule that catches what
   the other two cannot: run 12's `My name is Rani.` and
   `We have all info, need agreement.` carry no label and no newline -- they are
   just sentences jammed after a Telugu question mark, and no marker matches
   them. Both layers already say a turn ends on its one question ("End your turn
   with the single most useful question... Not two questions", core.md:44;
   "Ask one question per turn", outbound.md:42), so this enforces a documented
   rule rather than inventing one, and it removes the multi-question defect at
   the same time.

   Checked before adopting, against every reply runs 1-12 produced: 0 of 9 good
   replies were altered, and all 3 known-bad ones were cut correctly. The tag
   questions Layer 1 encourages (`కదా`, `అవునా`) survive because the house style
   joins them with a comma rather than a question mark.
4. Nothing is swallowed. The old filter returned early while its buffer was under
   40 characters and had no flush on response-end, so a short reply with no
   trailing newline could vanish entirely. `finish(...)` always drains.

Streaming, and why text is held back
------------------------------------
The LLM streams in fragments, so a marker arrives split across frames -- `"MO"`
then `"DE:"`. Scanning each fragment alone would never match. So the tail of the
buffer is retained until enough characters exist to rule out a partial marker,
and only the safe prefix is released. `HOLDBACK` is sized to the longest marker,
which costs a few characters of delay and never a whole sentence.

The history matters as much as the audio
----------------------------------------
This runs upstream of both the TTS and the assistant context aggregator, so the
cleaned text is what gets written into the conversation history. That is the
point. The aggregator appends whatever reaches it with no inspection
(`llm_response_universal.py:1974-1995`), and `StateInjector._refresh` only
strips system messages -- so an uncleaned blob was re-fed to the model on every
later turn, teaching it the format again. Run 12 shows the consequence: turn 4
was the 272-character script, turn 5 came back cut off, turn 6 was two
characters. Cleaning here breaks that loop.
"""

from __future__ import annotations

import re

# `MODE: ASK` / `mode:end` / `MODE : CLOSE` -- and any trailing junk on the line.
MODE_RE = re.compile(r"MODE\s*:\s*(ASK|CLOSE|END)\b[^\n]*\n?", re.IGNORECASE)

# Once one of these appears the model has stopped replying and begun writing
# dialogue. WRONG/RIGHT are the few-shot labels from the prompt layers, which the
# model has been observed to continue.
ROLE_LABEL_RE = re.compile(
    r"(?:^|\n|(?<=[.!?।]))\s*"
    r"(CUSTOMER|CALLER|USER|AGENT|ASSISTANT|BOT|WRONG|RIGHT)\s*:",
    re.IGNORECASE,
)

# Long enough that a marker beginning inside the retained tail is never released
# half-scanned. "ASSISTANT:" is 10; the MODE form with spaces and a value is ~14.
from api.services.vaani.speech_register import spoken  # noqa: E402

HOLDBACK = 24


class ReplySanitizer:
    """Incrementally cleans one reply. Construct one per LLM response."""

    def __init__(self, names: tuple[str, ...] = ()) -> None:
        """`names` are the people in the conversation.

        Needed because "విష్ణు అండి" can only be corrected to "విష్ణు గారు" by
        something that knows విష్ణు is a name. అండి is a sentence-final particle
        and belongs after a verb; గారు is the one that goes after a name, and
        run 305 said the wrong one four times.
        """
        self._names = tuple(n for n in names if n)
        self._buffer = ""
        # Discarded text is still accumulated, because the mode has to be read
        # out of it and a streamed marker arrives one character at a time.
        self._discarded = ""
        self._truncated = False
        self._asked = False
        self.mode: str | None = None
        self.removed: list[str] = []

    def feed(self, text: str) -> str:
        """Add a streamed fragment; return the part safe to speak now."""
        if self._truncated:
            # Truncation silences the reply; it must not blind us to the signal.
            # `MODE: END` is what hangs up the call, and on run 12 it arrived
            # AFTER the text that gets cut. Keep reading the mode out of the
            # discarded remainder, and speak none of it.
            self._discard(text)
            return ""
        self._buffer += text
        return self._drain(final=False)

    def finish(self) -> str:
        """Release whatever is left. Always drains, so nothing is swallowed."""
        if self._truncated:
            self._discard(self._buffer)
            self._buffer = ""
            return ""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        self._buffer = self._strip_modes(self._buffer)
        self._buffer = self._register(self._buffer, final=final)

        cut = ROLE_LABEL_RE.search(self._buffer)
        if cut:
            # Everything from the label onward is invented dialogue.
            self._truncated = True
            out = self._buffer[: cut.start()]
            dropped = self._buffer[cut.start():]
            self._discard(dropped)  # signal survives what speech does not
            self._buffer = ""
            return out

        # The turn ends on its question. Anything after it is a second question,
        # an invented caller line, or a note to self -- run 12 produced all three.
        q = self._buffer.find("?")
        if q >= 0:
            out = self._buffer[: q + 1]
            rest = self._buffer[q + 1:]
            self._asked = True
            self._buffer = ""
            if rest.strip():
                self._truncated = True
                self._discard(rest)  # signal survives what speech does not
            return out
        if self._asked:
            # A question was already released; nothing further belongs to it.
            if self._buffer.strip():
                self._truncated = True
                self._discard(self._buffer)
            self._buffer = ""
            return ""

        if final:
            out, self._buffer = self._buffer, ""
            return out

        # Retain a tail that could still turn out to be the start of a marker.
        if len(self._buffer) <= HOLDBACK:
            return ""
        out, self._buffer = self._buffer[:-HOLDBACK], self._buffer[-HOLDBACK:]
        return out

    def _register(self, text: str, *, final: bool) -> str:
        """Fix the spoken register, but only on words that are finished.

        The rewrite cannot run on the raw buffer, and the reason is a bug the
        existing colon test caught within the hour. Tokens arrive one character
        at a time, so mid-stream the buffer ends "...పది గంట" -- at which point
        గంట looks like a bare singular and is corrected to గంటలు. The remaining
        "లకు" then arrives and the caller hears "గంటలులకు", a word that exists
        in no language.

        HOLDBACK does not help: it stops a marker being released half-scanned,
        but this match is INSIDE the retained tail and is wrong only because
        more text is coming.

        So the buffer is split at the last whitespace and only the completed
        words are rewritten. A trailing fragment is left exactly as it is until
        something follows it, and on the final drain everything is rewritten
        because nothing more is coming.
        """
        if final:
            return spoken(text, self._names)
        cut = max(text.rfind(" "), text.rfind(chr(10)))
        if cut < 0:
            return text
        return spoken(text[:cut], self._names) + text[cut:]

    def _discard(self, text: str) -> None:
        """Bank text we will not speak, and mine it for the mode.

        Accumulated rather than scanned per fragment: the stream delivers
        `MODE: CLOSE` one character at a time, so a per-fragment regex would
        never match it.
        """
        self._discarded += text
        self._discarded = self._strip_modes(self._discarded)
        self.removed.append(text)

    def _strip_modes(self, text: str) -> str:
        """Remove every MODE line, recording the last mode seen."""
        while True:
            m = MODE_RE.search(text)
            if not m:
                return text
            self.mode = m.group(1).upper()
            self.removed.append(m.group(0))
            text = text[: m.start()] + text[m.end():]
