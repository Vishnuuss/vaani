"""Layer 3's reference material, charged only on the turn it is needed.

The problem
-----------
MB Solar's client prompt is 13,795 chars, of which 7,220 sit under a heading
that already says "## Reference — the rest of this document": eighteen FAQ
answers, the PM Surya Ghar scheme in full, and six objections. A caller asks
one or two of those eighteen. The model reads all eighteen, on every turn,
including the turn whose whole job is "which area do you live in?".

Measured on run 961 (13 Sep 2026), an 11-turn call:

    prompt tokens per turn        42,460
    the reference block is 21% of it       8,904 tokens/turn
    over the call                         97,945 tokens
    at 84.6% cached                        Rs 0.75 per call
                                           Rs 7,460 per 10,000 calls

MB Solar costs Rs 3.56 a call against Investment's Rs 1.40 on the same build.
A fifth of that difference is text re-read fifteen times for nothing.

This is a COST fix, and only a cost fix
----------------------------------------
It is not sold as a latency fix, because the evidence does not support one.
Runs 939 and 961 are the same agent with the same 34,429-char prompt and
measured 1.48s and 2.23s; half the gap to the faster agents sits in `endpoint`,
which happens before the LLM is called and cannot be touched from here. Groq
already gets `reasoning_effort=low` and `compile_prompt` already front-loads
the constants for cache locality, so the usual wins are spent. Any latency this
returns is a bonus and must be measured over several calls on ONE agent before
it is claimed.

Nothing is deleted. Every word the client wrote is still reachable; it is
reached on demand.

Why the triggers live in the client's config
---------------------------------------------
`coach.py` is the same idea for SALES TACTICS, and its catalogue is code
because tactics generalise across industries -- "one rebuttal, never two" is
true for solar and for loans. Solar FAQ does not generalise, and the shared
layers stay industry-neutral, so it cannot go there.

So the mechanism is here and is general; the subject knowledge stays in Layer
3 where the rest of the client's material lives. A row carries its own trigger:

    **Is there a subsidy? / How much subsidy?**
    TRIGGER: సబ్సిడీ|subsidy|స్కీమ్|scheme
    "PM Surya Ghar స్కీమ్ లో మొదటి two kW కి ..."

The bold line is for a human reading the config. `TRIGGER:` is the only part
this module interprets, and it is written by whoever knows what that client's
callers actually say. A row with no `TRIGGER:` is never selected -- silently
matching on the English heading would fire "how long" on a caller asking how
long installation takes AND on one asking how long panels last.

Precision over recall, for the reason `coach.py` gives: the model obeys the
trailing block over the prose above it, so a wrong row is worse than no row.
When nothing matches, the client's own catch-all still applies -- MB Solar's
is "అది నేను కనుక్కుని చెప్తాను అండి".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The heading that separates the operational half of Layer 3 from the half
# that is only consulted. Matched case-insensitively at the start of a line,
# and NOT invented here -- the client's document already says this.
REFERENCE_HEADING = re.compile(r"^##\s*Reference\b", re.I | re.M)

# One row per bold heading, running to the next bold heading or the next "##".
_ROW = re.compile(r"^\*\*(?P<title>.+?)\*\*[ \t]*\n(?P<body>.*?)"
                  r"(?=\n\*\*|\n##\s|\Z)", re.S | re.M)

_TRIGGER = re.compile(r"^TRIGGER:[ \t]*(?P<pattern>.+)$", re.I | re.M)

# Two rows is the ceiling, for `coach.py`'s reason: three tactical blocks plus
# STILL_NEED plus the next question is how a two-sentence reply becomes a
# paragraph. A FAQ answer is longer than a coach line, so the char budget is
# larger -- these are sentences the agent may say almost verbatim.
MAX_ROWS = 2
MAX_CHARS = 700


@dataclass(frozen=True)
class Row:
    """One question the caller might ask, and the answer the client wrote."""

    title: str
    body: str
    pattern: re.Pattern | None

    def matches(self, text: str) -> bool:
        return bool(self.pattern and self.pattern.search(text or ""))


def split(prompt: str) -> tuple[str, str]:
    """(what the agent carries every turn, what it consults on demand).

    A prompt with no reference heading is returned whole in the first slot.
    That is the safe direction: an agent that has not been migrated keeps
    behaving exactly as it does today.
    """
    text = prompt or ""
    m = REFERENCE_HEADING.search(text)
    if not m:
        return text, ""
    return text[:m.start()].rstrip(), text[m.start():].strip()


def parse(reference: str) -> list[Row]:
    """Every bold-headed row in the reference half, in document order."""
    rows: list[Row] = []
    for m in _ROW.finditer(reference or ""):
        body = m.group("body")
        trig = _TRIGGER.search(body)
        pattern = None
        if trig:
            raw = trig.group("pattern").strip()
            try:
                pattern = re.compile(raw, re.I)
            except re.error:
                # A broken regex in config must not take the call down. The row
                # simply never fires, which is the same as not having written
                # it -- and the client's catch-all still answers the caller.
                pattern = None
            body = _TRIGGER.sub("", body)
        body = "\n".join(ln for ln in body.splitlines() if ln.strip()).strip()
        if body:
            rows.append(Row(title=m.group("title").strip(), body=body,
                            pattern=pattern))
    return rows


def lookup(rows: list[Row], text: str, exclude: object = ()) -> list[str]:
    """The reference lines to append to the state block for this utterance.

    `exclude` is the set of row titles already given in this call. Handing the
    same answer twice is the model being told to repeat itself.
    """
    said = (text or "").strip()
    if not said:
        return []
    seen = set(exclude or ())
    out: list[str] = []
    for row in rows:
        if row.title in seen or not row.matches(said):
            continue
        line = f"REFERENCE ({row.title}): {row.body}"
        if len(line) > MAX_CHARS:
            # Too long to spend on one turn. Not an error -- the catch-all
            # covers it, and a 2,000-character block in the most authoritative
            # position would be read out.
            continue
        out.append(line)
        seen.add(row.title)
        if len(out) >= MAX_ROWS:
            break
    return out
