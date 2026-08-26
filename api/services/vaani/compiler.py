"""Compiles the 4-layer system prompt.

Deterministic assembly, not generation. A client's agent is a COMPILE step, so
prompt quality is not a lottery re-rolled per client.

  Layer 1  persona & voice     per language   constant
  Layer 2  sales psychology    global         constant  <- the moat
  Layer 3  business            per client     generated from the brief
  Layer 4  mission             per agent type template

Layers 1+2 are byte-identical across every call for a given language, so they
sit at the FRONT of the prompt and land in the provider's cached prefix. That is
also what makes speculation cheap -- a cancelled speculative call pays almost
nothing when 80% of its input is cached.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from pathlib import Path

LAYERS_DIR = Path(__file__).parent / "layers"


@dataclass
class Brief:
    """The per-client inputs. Mirrors clients/<name>.yaml."""

    business: str
    industry: str = ""
    agent_type: str = "outbound"
    language: str = "te-IN"
    agent_name: str = "ప్రియ"
    topic: str = ""
    lead_source: str = ""
    questions: list[str] = field(default_factory=list)
    disqualify_if: list[str] = field(default_factory=list)
    success: str = ""
    products: str = ""
    customer_situation: str = ""
    objection_playbook: str = ""
    buying_signals: list[str] = field(default_factory=list)
    vocabulary: list[str] = field(default_factory=list)
    proof: str = ""
    compliance: list[str] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Brief":
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def field_names(self) -> list[str]:
        """Field keys for CallState and the extractor.

        A question may be a plain string, or a mapping with an explicit
        `field`. Prefer the explicit form -- derived names like
        `rent_your_home` are ugly and make the extractor's job harder.
        """
        out = []
        for i, q in enumerate(self.questions):
            if isinstance(q, dict):
                out.append(q.get("field") or f"answer_{i+1}")
            else:
                words = [w.lower() for w in str(q).replace("?", "").split() if w.isalpha()]
                out.append("_".join(words[-3:]) if words else f"answer_{i+1}")
        return out

    @property
    def question_texts(self) -> list[str]:
        return [q.get("ask", "") if isinstance(q, dict) else str(q)
                for q in self.questions]


def _read(*parts: str) -> str:
    path = LAYERS_DIR.joinpath(*parts)
    if not path.exists():
        raise FileNotFoundError(f"missing prompt layer: {path}")
    return path.read_text(encoding="utf-8").strip()


def _business_layer(brief: Brief) -> str:
    """Layer 3 -- rendered from the brief. The only genuinely per-client text."""
    lines = [f"# Layer 3 — Business: {brief.business}", ""]
    lines.append(f"You are calling on behalf of **{brief.business}**.")
    lines.append(f"Your name is {brief.agent_name}.")
    if brief.topic:
        lines.append(f"The subject of the call is {brief.topic}.")
    if brief.products:
        lines += ["", "## What we offer", brief.products]
    if brief.customer_situation:
        lines += ["", "## Who you are calling", brief.customer_situation]
    if brief.vocabulary:
        lines += ["", "## Words real customers use for this",
                  "Use these, not literary equivalents: "
                  + ", ".join(brief.vocabulary) + "."]
    if brief.lead_source:
        lines += ["", "## Where their number came from",
                  "If they ask how you got their number, say exactly this and "
                  "then offer to remove them: " + brief.lead_source]
    if brief.proof:
        lines += ["", "## Proof you may use (real, never embellished)", brief.proof]
    if brief.questions:
        lines += ["", "## You must find out"]
        # Question text ONLY. The field key used to be appended in brackets and
        # the model read it aloud -- a live turn ended "...savings scheme
        # (save_with_any)". The extractor gets its keys from the brief directly;
        # they never belong in something the agent can say.
        lines += [f"- {q}" for q in brief.question_texts]
    if brief.objection_playbook:
        lines += ["", "## Objections specific to this industry",
                  "The general playbook in Layer 2 still applies. These are the "
                  "ones peculiar to this business:", brief.objection_playbook]
    if brief.buying_signals:
        lines += ["", "## Buying signals in this industry",
                  "Any of these means stop qualifying and close:"]
        lines += [f"- {b}" for b in brief.buying_signals]
    if brief.disqualify_if:
        lines += ["", "## Disqualifiers — stop selling and close warmly if true"]
        lines += [f"- {d}" for d in brief.disqualify_if]
    if brief.compliance:
        lines += ["", "## Compliance — absolute, no exceptions"]
        lines += [f"- {c}" for c in brief.compliance]
    lines += ["", "If you do not know something, say so and offer to find out. "
                  "Never invent a price, a rate, a timeline or a customer."]
    return "\n".join(lines)


def compile_prompt(brief: Brief) -> str:
    """Assemble the full system prompt. Constants first, for cache locality."""
    persona = _read("01_persona", f"{brief.language}.md")
    psychology = _read("02_psychology", "core.md")
    mission = _read("04_mission", f"{brief.agent_type}.md")

    mission = (mission
               .replace("{business}", brief.business)
               .replace("{agent_name}", brief.agent_name)
               .replace("{topic}", brief.topic or "our service")
               .replace("{success_definition}",
                        brief.success or "a specific, agreed next step"))

    return "\n\n---\n\n".join([
        persona,        # constant  ] cached prefix
        psychology,     # constant  ]
        _business_layer(brief),
        mission,
    ])


def build_messages(system_prompt: str, state_block: str,
                   history: list[dict], user_text: str) -> list[dict]:
    """Assemble one turn's messages.

    The state block goes in its own system message AFTER the history, so it is
    the freshest instruction the model sees and cannot be buried by a long
    conversation.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    if user_text:
        messages.append({"role": "user", "content": user_text})
    messages.append({"role": "system", "content": state_block + MODE_PROTOCOL})
    return messages


# --- turn mode ---------------------------------------------------------------
# Detecting "the caller is ready to book" from the caller's own words does not
# generalise. A jewellery customer says "ఏ రోజులో మీరు రావచ్చు?" and a solar
# customer says something else entirely; a regex that covers one misses the
# other, and there is no end to the list.
#
# The model already knows -- it just had no way to say so. One declared token
# per turn makes the intent explicit and enforceable, costs about three tokens,
# and is stripped before anything reaches the speech engine.
MODE_PROTOCOL = """

OUTPUT FORMAT — the first line of your reply is always exactly one of:
MODE: ASK    — the call is live and you are moving it forward
MODE: CLOSE  — they are ready; you are agreeing a specific time
MODE: END    — the call is over; you are saying goodbye and nothing else

Then a blank line, then what you say out loud. The MODE line is never spoken.

END is expensive — it hangs up on a customer. Choose it ONLY when one of these
is plainly true:
  - they refused a second time, or asked to be removed
  - they are angry and want the call to stop
  - they fail a listed disqualifier
  - a child answered
  - a next step is already agreed and there is nothing left to say

**If they are still asking you questions, they have not left. Never END.**
A caller who is testing you, arguing with you, demanding detail or pushing on
price is an ENGAGED caller — that is ASK, not END. Ending on them throws away
a live customer, which is the most expensive mistake in this whole document."""

_MODE_RE = re.compile(r"^\s*MODE:\s*(ASK|CLOSE|END)\b[^\n]*\n?", re.IGNORECASE)


def parse_mode(reply: str) -> tuple[str, str]:
    """Split a raw reply into (mode, spoken text).

    Defaults to ASK when the model forgets the line -- a missing declaration
    must never silently mute the agent.
    """
    m = _MODE_RE.match(reply or "")
    if not m:
        return "ASK", (reply or "").strip()
    return m.group(1).upper(), _MODE_RE.sub("", reply, count=1).strip()
