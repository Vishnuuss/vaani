"""Run a simulated call: a persona LLM against the real agent brain.

TEXT TRANSPORT ONLY. This must never touch telephony -- it costs a few paise per
call and runs 30 in parallel, which is the entire point. A simulated call that
placed a real call would make the release gate unaffordable and dangerous.

What is real here: the compiled 4-layer prompt, the live state block, the
extractor, and the actual production model. What is missing: audio, and
therefore endpointing and barge-in. Those are measured separately by the
latency bench and the adversarial suite.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..brain import compiler, extractor, guardrails, triage, state as state_mod
from .personas import Persona

MAX_TURNS = 14

CUSTOMER_SYSTEM = """You are role-playing a person in India who has just answered \
an unexpected phone call. You are NOT an assistant.

{brief}

Rules:
- Reply in natural spoken Telugu, the way a real person speaks on a phone.
  Mix in English or Hindi words where a real Telugu speaker naturally would.
- ONE or TWO short sentences per turn. Never explain yourself at length.
- Never break character. Never be helpful for its own sake.
- You do not know or care what the caller's job targets are.
- State your position IN YOUR OWN WORDS first. You may only reply [HANGUP]
  from your THIRD turn onward -- never on the first two turns, even if you are
  furious or want to be removed. Say the thing, then hang up if they mishandle it.
- If you decide the call is over, reply with exactly: [HANGUP]
"""


@dataclass
class SimResult:
    persona: Persona
    transcript: list[dict] = field(default_factory=list)
    state: state_mod.CallState | None = None
    hung_up: bool = False
    turns: int = 0
    error: str = ""

    def render(self) -> str:
        lines = []
        for msg in self.transcript:
            who = "AGENT   " if msg["role"] == "assistant" else "CUSTOMER"
            lines.append(f"{who}: {msg['content']}")
        return "\n".join(lines)


async def _customer_turn(llm, persona: Persona, transcript: list[dict]) -> str:
    """The persona speaks. Roles are flipped: the agent's lines are 'user'."""
    flipped = [
        {"role": "user" if m["role"] == "assistant" else "assistant",
         "content": m["content"]}
        for m in transcript
    ]
    messages = [{"role": "system",
                 "content": CUSTOMER_SYSTEM.format(brief=persona.brief)}] + flipped
    return (await llm.complete(messages, max_tokens=90, temperature=0.9)).strip()


async def run_call(llm, brief: compiler.Brief, persona: Persona) -> SimResult:
    """One full simulated call. Never raises -- failures are recorded."""
    result = SimResult(persona=persona)
    try:
        system_prompt = compiler.compile_prompt(brief)
        state = state_mod.CallState(
        required_fields=brief.field_names,
        questions=dict(zip(brief.field_names, brief.question_texts)))
        result.state = state

        greeting = (f"నమస్కారం సార్, నేను {brief.agent_name}, {brief.business} నుంచి "
                    f"మాట్లాడుతున్నాను. ఒక్క రెండు నిమిషాలు మాట్లాడొచ్చా?")
        transcript = [{"role": "assistant", "content": greeting}]

        limit = min(MAX_TURNS, persona.hangs_up_after)
        for _ in range(limit):
            customer = await _customer_turn(llm, persona, transcript)
            if "[HANGUP]" in customer:
                result.hung_up = True
                break
            transcript.append({"role": "user", "content": customer})

            # Hard stops are detected SYNCHRONOUSLY, before the reply exists.
            # The async extractor is one turn too late for these.
            triage.apply(state, customer)

            messages = compiler.build_messages(
                system_prompt, state.render(), transcript[:-1], customer)
            raw = (await llm.complete(messages, max_tokens=200,
                                      temperature=0.7)).strip()
            mode, reply = compiler.parse_mode(raw)

            # Deterministic compliance check before the reply is "spoken".
            # One retry, then a safe scripted line -- never ship a violation.
            # The model's own declaration, plus anything triage already latched.
            # Either is sufficient -- the declaration generalises across
            # industries, the latch catches the cases it forgets.
            closing = guardrails.must_close(state) or mode == "END"
            if mode == "END":
                state.must_end = True
            report = guardrails.check(reply, closing=closing)
            if not report.ok:
                retry = messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "system", "content": report.correction_note},
                ]
                _, reply = compiler.parse_mode(
                    (await llm.complete(retry, max_tokens=200,
                                        temperature=0.5)).strip())
                if not guardrails.check(reply, closing=closing).ok:
                    reply = (guardrails.SAFE_CLOSE if closing
                             else guardrails.SAFE_FALLBACK)
            transcript.append({"role": "assistant", "content": reply})

            # Same extraction the live gateway runs, so the state block the
            # agent sees in simulation matches production.
            data = await extractor.extract(
                llm, brief.field_names, brief.disqualify_if, customer, reply)
            extractor.apply_to_state(state, data, brief.field_names)
            state.advance()
            result.turns += 1

        result.transcript = transcript
        if result.turns == 0:
            # The persona hung up before the agent could say anything back.
            # Scoring this would blame the agent for a call it never had.
            result.error = "no interaction: persona ended before any agent reply"
    except Exception as exc:  # noqa: BLE001 - one bad call must not kill the suite
        result.error = f"{type(exc).__name__}: {exc}"[:200]
    return result


async def run_all(llm, brief: compiler.Brief, personas: list[Persona],
                  concurrency: int = 6) -> list[SimResult]:
    """Run the persona bank with bounded concurrency.

    Bounded because the provider will rate-limit 30 concurrent streams, and a
    429 storm would look like an agent failure when it is a harness failure.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(p: Persona) -> SimResult:
        async with sem:
            return await run_call(llm, brief, p)

    return await asyncio.gather(*(one(p) for p in personas))
