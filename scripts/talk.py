"""Talk to the agent in the terminal -- no phone call, no telephony cost.

This exercises the real brain: the compiled 4-layer prompt, the live state
block, and the real LLM. Only the audio path is missing. Use it to tune the
prompt before spending a single rupee on calls.

    python scripts/talk.py
    python scripts/talk.py --brief clients/bswealth.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vaani.brain import compiler, extractor, state as state_mod  # noqa: E402
from vaani.core import endpointer as ep  # noqa: E402
from vaani.core.llm import LLMClient, GROQ, SARVAM  # noqa: E402


def load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", default="clients/bswealth.yaml")
    parser.add_argument("--provider", choices=["sarvam", "groq"], default="sarvam")
    args = parser.parse_args()

    load_env()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    brief = compiler.Brief.from_yaml(args.brief)
    system_prompt = compiler.compile_prompt(brief)
    call_state = state_mod.CallState(required_fields=brief.field_names)
    provider = SARVAM if args.provider == "sarvam" else GROQ

    print(f"\n  {brief.business} / {brief.agent_type} / {brief.language}")
    print(f"  prompt: {len(system_prompt)//4} tokens   model: {provider.model}")
    print("  type your reply as the customer; blank line or 'q' to quit\n")

    history: list[dict] = []
    greeting = (f"నమస్కారం సార్, నేను {brief.agent_name}, {brief.business} నుంచి "
                f"మాట్లాడుతున్నాను. ఒక్క రెండు నిమిషాలు మాట్లాడొచ్చా?")
    print(f"  AGENT: {greeting}\n")
    history.append({"role": "assistant", "content": greeting})

    async with LLMClient(primary=provider, fallback=None) as llm:
        while True:
            try:
                user = input("  YOU:   ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user or user.lower() in ("q", "quit", "exit"):
                break

            # Show what our endpointer would have decided for this text -- the
            # cheapest way to sanity-check the Telugu rules without a phone.
            decision = ep.classify(user)
            history.append({"role": "user", "content": user})
            messages = compiler.build_messages(
                system_prompt, call_state.render(), history[:-1], user)

            t0 = time.perf_counter()
            first = None
            reply = []
            async for delta in llm.stream(messages):
                if first is None:
                    first = (time.perf_counter() - t0) * 1000
                reply.append(delta)
            text = "".join(reply).strip()
            history.append({"role": "assistant", "content": text})

            # Off the critical path -- the reply is already out. In the live
            # gateway this is fire-and-forget; here we await it so the state
            # block printed below reflects the turn we just took.
            await extractor.spawn(llm, call_state, brief.field_names,
                                  brief.disqualify_if, user, text)
            call_state.advance()

            print(f"\n  AGENT: {text}")
            print(f"         [first token {first:.0f}ms | endpointer: "
                  f"{decision.completion.value} wait {decision.wait_ms}ms "
                  f"({decision.reason}) | phase {call_state.phase.value}]\n")

    print("\n  --- final state ---")
    print("  " + call_state.render().replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
