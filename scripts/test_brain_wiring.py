"""Prove the live brain wiring without touching a vendor.

The gateway cannot be tested end to end right now -- Sarvam returns 402, and it
is the only Telugu STT in the stack. That is a funding problem, not a code
problem, and it must not leave the brain wiring unverified until someone tops
the account up.

So this exercises the two processors directly with stub frames:

  1. the state block stays LAST in the context, and refreshes each turn
  2. triage latches through the injector, so the state block closes the call
  3. the MODE line is stripped and never reaches TTS -- the failure here would
     be the agent literally saying "mode ask" down the phone
  4. MODE: END latches must_end
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vaani.brain.compiler import Brief                       # noqa: E402
from vaani.gateway.brain_processor import ReplyFilter, StateInjector  # noqa: E402


class FakeContext:
    def __init__(self, messages):
        self._messages = list(messages)

    @property
    def messages(self):
        return list(self._messages)

    def set_messages(self, messages):
        self._messages = list(messages)


class Collector:
    """Stands in for the rest of the pipeline."""

    def __init__(self):
        self.text = ""


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    return ok


async def main() -> int:
    brief = Brief.from_yaml(ROOT / "clients" / "bs-wealth-finance.yaml")
    system_prompt = "SYSTEM PROMPT BODY"
    ctx = FakeContext([{"role": "system", "content": system_prompt}])

    injector = StateInjector(brief, ctx, system_prompt)
    ok = True

    # --- 1. state block is last, and only one of them ---------------------
    injector._refresh()
    ctx._messages.append({"role": "user", "content": "హలో"})
    injector._refresh()
    msgs = ctx.messages
    blocks = [m for m in msgs if "STILL_NEED" in str(m.get("content", ""))]
    ok &= check("state block is last", "STILL_NEED" in str(msgs[-1]["content"]))
    ok &= check("exactly one state block", len(blocks) == 1, f"{len(blocks)} found")
    ok &= check("system prompt survives refresh",
                msgs[0]["content"] == system_prompt)
    ok &= check("user turn survives refresh",
                any(m.get("role") == "user" for m in msgs))
    ok &= check("MODE protocol attached",
                "MODE: ASK" in str(msgs[-1]["content"]))

    # --- 2. triage latches through the injector ---------------------------
    injector.state.refusals = 1                       # one refusal already
    from vaani.brain import triage
    triage.apply(injector.state, "వద్దు సార్")         # the second one
    injector._refresh()
    block = str(ctx.messages[-1]["content"])
    ok &= check("second refusal closes the call",
                injector.state.must_end and "STOP." in block)
    ok &= check("checklist suppressed when closing",
                "STILL_NEED: []" in block)

    # --- 3. the MODE line never reaches TTS -------------------------------
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame)

    rf = ReplyFilter(injector)
    out = Collector()

    async def fake_push(frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            out.text += frame.text

    rf.push_frame = fake_push                          # type: ignore[assignment]
    rf._buffer, rf._mode_done, rf._spoken = "", False, ""

    # Stream it the way an LLM actually does: in small pieces.
    for piece in ["MODE:", " ASK", "\n", "\n", "అర్థమైంది", " సార్."]:
        await rf.process_frame(LLMTextFrame(piece), None)

    ok &= check("MODE line stripped from speech", "MODE" not in out.text,
                repr(out.text))
    ok &= check("spoken text intact", "అర్థమైంది సార్." in out.text, repr(out.text))

    # --- 4. MODE: END latches must_end ------------------------------------
    injector.state.must_end = False
    rf2 = ReplyFilter(injector)
    out2 = Collector()

    async def fake_push2(frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            out2.text += frame.text

    rf2.push_frame = fake_push2                        # type: ignore[assignment]
    rf2._buffer, rf2._mode_done, rf2._spoken = "", False, ""
    for piece in ["MODE: END\n", "\n", "థాంక్యూ సార్."]:
        await rf2.process_frame(LLMTextFrame(piece), None)
    ok &= check("MODE: END latches must_end", injector.state.must_end)
    ok &= check("END text still spoken", "థాంక్యూ" in out2.text, repr(out2.text))

    print("\n  " + ("ALL CHECKS PASSED -- brain wiring is live-ready"
                    if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
