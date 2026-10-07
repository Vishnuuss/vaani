"""Run the persona bank against an agent and print the release gate.

    python -m vaani.sim.run                          # all personas
    python -m vaani.sim.run --brief clients/x.yaml
    python -m vaani.sim.run --only angry,renter,busy
    python -m vaani.sim.run --save .tmp/sim          # dump transcripts

Text transport only. No telephony, no billed calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from vaani.brain import compiler  # noqa: E402
from vaani.core.llm import LLMClient, GROQ, SARVAM  # noqa: E402
from vaani.sim import personas as P  # noqa: E402
from vaani.sim.score import Report, score_call  # noqa: E402
from vaani.sim.simulate import run_all  # noqa: E402


def load_env() -> None:
    env = REPO / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    ap = argparse.ArgumentParser(description="Vaani agent release gate")
    ap.add_argument("--brief", default="clients/bswealth.yaml")
    ap.add_argument("--only", help="comma-separated persona keys")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--save", help="directory to write transcripts into")
    ap.add_argument("--provider", choices=["sarvam", "groq"], default="groq")
    ap.add_argument("--model", help="override the provider's model id")
    args = ap.parse_args()

    load_env()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    brief = compiler.Brief.from_yaml(REPO / args.brief)
    # Shared archetypes bound to this client's industry, plus one generated
    # persona per declared disqualifier. Nothing here is written per client.
    available = P.for_brief(brief)
    chosen = available
    if args.only:
        keys = {k.strip() for k in args.only.split(",")}
        chosen = [p for p in available if p.key in keys]
        if not chosen:
            print(f"no personas matched {keys}")
            return 2

    provider = SARVAM if args.provider == "sarvam" else GROQ
    if args.model:
        import dataclasses
        # reasoning_effort / reasoning_format are gpt-oss-only knobs; sending
        # them to any other model is a 400.
        if "gpt-oss" in args.model:
            extra = provider.extra
        elif "qwen" in args.model:
            # qwen3 on Groq is reasoning-first and streams <think> into content
            # unless this is set. reasoning_format is silently ignored for it --
            # it returns empty content instead, which reads as a dead agent.
            extra = (("reasoning_effort", "none"),)
        else:
            extra = ()
        provider = dataclasses.replace(provider, model=args.model, extra=extra)
    print(f"\n  agent   : {brief.business} / {brief.agent_type} / {brief.language}")
    print(f"  model   : {provider.model}")
    print(f"  personas: {len(chosen)}   concurrency: {args.concurrency}")
    print("  running simulated calls (text only, no telephony) ...\n")

    async with LLMClient(primary=provider, fallback=None) as llm:
        results = await run_all(llm, brief, chosen, concurrency=args.concurrency)
        print("  scoring ...")
        sem = asyncio.Semaphore(args.concurrency)

        async def judge(r):
            async with sem:
                return await score_call(llm, r, brief)

        scores = await asyncio.gather(*(judge(r) for r in results))

    report = Report(scores=list(scores))
    print(report.render())

    if args.save:
        out = Path(args.save)
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for r in results:
            (out / f"{stamp}-{r.persona.key}.txt").write_text(
                f"# {r.persona.name}\n# {r.persona.brief}\n\n{r.render()}\n",
                encoding="utf-8")
        (out / f"{stamp}-scores.json").write_text(
            json.dumps([{
                "persona": s.persona_key, "score": round(s.weighted, 2),
                "criteria": s.criteria, "violations": s.violations,
                "worst": s.worst_moment, "fix": s.fix, "error": s.error,
            } for s in scores], indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  transcripts saved to {out}\n")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
