"""The live state block -- what replaces the node graph.

A node graph makes the state machine GENERATE the reply, which is backwards:
when the caller says something no edge matches, there is no transition and the
agent goes silent. That is the Dograh bug, and it is structural.

Here the state machine only CONSTRAINS. The model always generates, so there is
never an edge to fall off. This block is ~80 tokens injected fresh each turn --
it tells the agent where it is and what it still owes, without ever blocking it
from answering whatever was actually said.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum


class Phase(Enum):
    OPENING = "opening"
    QUALIFYING = "qualifying"
    PITCHING = "pitching"
    CLOSING = "closing"
    WRAPPING = "wrapping"


# Telugu marks a yes/no question by suffixing the AA vowel sign to the verb --
# వస్తుందా, దొరుకుతుందా, పెట్టాలా -- with no "?" and no change in
# word order. Looking for "?" or wh-words alone caught 5 of 8 real caller
# questions and missed exactly the ones that matter: "will I get power in the
# rainy season", "can I get a loan", "do I need a battery".
#
# The statement forms end in a different vowel sign (వస్తుంది "it comes"),
# so the ending is what separates them.
_QUESTION_WORDS = re.compile(
    r"(\?|ఎంత|ఎక్కడ|ఎప్పుడు|ఎలా|ఏమిటి|ఏంటి|ఏమి|ఎందుకు|ఎవరు|ఎన్ని|ఏది|ఏవి|"
    r"(what|when|where|how|why|which|who|can|do|does|is|are))",
    re.IGNORECASE)

# Ends on the AA sign: the Telugu interrogative particle.
_QUESTION_PARTICLE = re.compile(r"[ఀ-౿]ా\s*[?.!]?\s*$")


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_QUESTION_WORDS.search(t)) or bool(_QUESTION_PARTICLE.search(t))


@dataclass
class CallState:
    required_fields: list[str] = field(default_factory=list)
    questions: dict = field(default_factory=dict)   # field -> the actual question
    known: dict[str, str] = field(default_factory=dict)
    objections: list[str] = field(default_factory=list)
    phase: Phase = Phase.OPENING
    turn: int = 0
    started_at: float = field(default_factory=time.time)
    disqualified: bool = False
    disqualify_reason: str = ""
    next_step_agreed: bool = False   # a visit/callback/time has been accepted
    buying_signal: bool = False      # caller asked to book, or asked a closing question
    refusals: int = 0                # plain refusals so far; the 2nd ends the call
    no_more_questions: bool = False  # caller explicitly asked to stop being asked
    must_end: bool = False           # removal requested, hostile, or fraud accusation
    end_reason: str = ""

    @property
    def still_need(self) -> list[str]:
        return [f for f in self.required_fields if f not in self.known]

    @property
    def elapsed_s(self) -> int:
        return int(time.time() - self.started_at)

    def learn(self, field_name: str, value: str) -> None:
        if value:
            self.known[field_name] = value

    def note_objection(self, kind: str) -> None:
        if kind not in self.objections:
            self.objections.append(kind)

    # What the caller said on the turn just gone, so the acknowledgement has
    # something concrete to refer to instead of being generic.
    last_user_text: str = ""
    # What WE have already asked. Run 96 asked the same question four times and
    # the caller said "you told me nothing"; the model cannot avoid repeating
    # itself if it is never shown what it already said.
    asked: list = field(default_factory=list)

    def advance(self) -> None:
        """Move the phase forward based on what we actually know.

        Deliberately simple and deterministic. The agent decides what to SAY;
        this only tracks where the call has got to.
        """
        self.turn += 1
        if self.disqualified or self.next_step_agreed or self.must_end:
            self.phase = Phase.WRAPPING
        elif self.phase is Phase.OPENING and self.turn >= 1:
            self.phase = Phase.QUALIFYING
        elif self.phase is Phase.QUALIFYING and not self.still_need:
            self.phase = Phase.PITCHING
        elif self.phase is Phase.PITCHING and self.turn >= 8:
            self.phase = Phase.CLOSING

    def render(self) -> str:
        """The compact block injected into the prompt each turn.

        This block is the LAST thing the model sees, which makes it the most
        authoritative thing in the context -- more so than 3,000 tokens of prose
        further up. That is exactly why the hard behavioural constraints live
        here and not only in the prompt layers: listing STILL_NEED at the end of
        the context reliably makes the model ask for those fields, even when the
        prose says not to. So once the call is won or lost, we stop listing them.
        """
        lines = [f"PHASE: {self.phase.value}", f"KNOWN: {self.known or '{}'}"]

        # Any ending state MUST suppress the checklist. The 30-persona run showed
        # that simply listing STILL_NEED at the end of the context makes the model
        # ask for those fields -- even right after it agreed to remove the caller
        # from the list. That produced 8 of 12 compliance violations.
        if self.must_end:
            lines.append(f"STILL_NEED: [] -- STOP. {self.end_reason} "
                         "Say one short closing sentence and END THE CALL. "
                         "Ask NOTHING. Pitch NOTHING.")
        elif self.disqualified:
            lines.append("STILL_NEED: [] -- DISQUALIFIED. Do not ask anything "
                         "further and do not sell. Close warmly in one sentence.")
        elif self.next_step_agreed:
            lines.append("STILL_NEED: [] -- NEXT STEP IS AGREED. Do not ask "
                         "anything further. Thank them and end the call NOW. "
                         "Remaining details are collected at the visit.")
        elif self.buying_signal:
            lines.append("STILL_NEED: [] -- CALLER IS READY TO BOOK. Stop "
                         "qualifying. Offer a specific time and close.")
        elif self.no_more_questions:
            lines.append("STILL_NEED: [] -- THE CALLER HAS ASKED YOU TO STOP "
                         "ASKING QUESTIONS. Ask nothing at all. Answer what "
                         "they raised, or offer a time. Nothing else.")
        else:
            lines.append(f"STILL_NEED: {self.still_need or '[]'}")
            # Field KEYS are meaningless to the model -- it was being handed
            # `save_with_any` and left to invent a question from it. Spell out
            # the next one in the client's own words.
            nxt = self.still_need[0] if self.still_need else ""
            if nxt and self.questions.get(nxt):
                lines.append(f'NEXT QUESTION TO ASK: "{self.questions[nxt]}"')
                # The client's complaint, in one word: "no confirmations". The
                # reference agent opens nearly every turn with a two-word
                # acknowledgement -- "మంచిది", "సరేనండి", "చాలా సంతోషమండి" --
                # before asking anything. Ours went straight to the next
                # question and the caller said "you told me nothing".
                #
                # It lives here rather than in the prose layers because this
                # block is the last thing the model reads, and the same
                # instruction has been in Layer 2 all along without being obeyed.
                # Capped at two words on purpose: a long acknowledgement is
                # audio the caller waits through on every single turn.
                if self.asked:
                    recent = "; ".join(f'"{a}"' for a in self.asked[-3:])
                    lines.append(
                        f"ALREADY ASKED (do NOT ask any of these again): {recent}"
                    )
                    lines.append(
                        "If they did not answer the last one, do NOT repeat it. "
                        "Say you could not hear them and ask them to say it "
                        "again -- once, in different words."
                    )
                # A caller who asked something must be ANSWERED before anything
                # else. Layer 2 has said "Answer, then ask -- never defer" from
                # the beginning and it lost every time, because this block is
                # the last thing the model reads and it was ending with "NEXT
                # QUESTION TO ASK". Measured on the live agent: asked
                # "సోలార్ ప్యానెల్ ఎన్ని సంవత్సరాలు వస్తుంది?", "వర్షాకాలంలో
                # కరెంట్ వస్తుందా?", "లోన్ దొరుకుతుందా?" -- every single one got
                # "అర్థమైంది. మీ నెలవారీ బిల్లు ఎంత?" and no answer at all.
                # The acknowledgement made it worse: it now says "understood"
                # and then ignores them.
                if _is_question(self.last_user_text):
                    lines.append(
                        "THEY ASKED YOU A QUESTION. Respond to it FIRST, in "
                        "one or two short sentences, then ask your own.
"
                        "  - If your business facts cover it, answer from them.
"
                        "  - If they do NOT, say so out loud: that you will "
                        "have the team confirm. That is a complete, acceptable "
                        "answer.
"
                        "  - Never invent a number, a price, a saving, a "
                        "location, an address or a brand.
"
                        "MOVING TO YOUR QUESTION WITHOUT RESPONDING TO THEIRS "
                        "IS NOT ALLOWED. Saying only \"అర్థమైంది\" and then "
                        "asking your question counts as ignoring them, and it "
                        "is the fastest way to lose this call."
                    )
                if self.last_user_text:
                    lines.append(
                        f'THEY JUST SAID: "{self.last_user_text[:80]}"  -- '
                        "open with a TWO-WORD acknowledgement of it "
                        "(మంచిది / సరేనండి / అర్థమైంది), then ask the question. "
                        "Never more than two words."
                    )
        if self.objections:
            lines.append(f"OBJECTIONS_RAISED: {self.objections}")
        if self.disqualified and not self.must_end:
            lines.append(f"DISQUALIFIED: {self.disqualify_reason}")
        lines.append(f"TURN: {self.turn}   CALL_ELAPSED: {self.elapsed_s}s")
        return "\n".join(lines)
