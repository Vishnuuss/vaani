"""The agent factory: a business name and a few questions become an agent.

    python -m vaani.factory.build \
        --name "Acme Insurance" \
        --industry "term life insurance" \
        --type outbound \
        --language te-IN \
        --question "Do you already have a policy?" \
        --question "What is your age?" \
        --question "When are you looking to buy?"

What happens:

  [1/3] research the industry      -> .tmp/profiles/<slug>.json
  [2/3] write the client brief     -> clients/<slug>.yaml
  [3/3] compile the system prompt  -> one prompt, no nodes

Layers 1, 2 and 4 -- voice, sales psychology, mission -- are shared by every
agent ever built and are never touched here. Only Layer 3 is per-client, and it
is generated, not written. That is the whole point: a new client inherits the
accumulated psychology for free and only contributes facts.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vaani.brain.compiler import Brief, compile_prompt      # noqa: E402
from vaani.factory.research import IndustryProfile, research  # noqa: E402

CLIENTS_DIR = ROOT / "clients"
PROFILES_DIR = ROOT / ".tmp" / "profiles"

# Sensible default voice names per language. Overridable; the name is only ever
# spoken in the opening line.
DEFAULT_AGENT_NAME = {
    "te-IN": "ప్రియ", "hi-IN": "प्रिया", "ta-IN": "பிரியா",
    "kn-IN": "ಪ್ರಿಯಾ", "en-IN": "Priya",
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "client"


def field_key(question: str, index: int) -> str:
    """A stable snake_case key for the extractor to write into."""
    words = re.findall(r"[a-zA-Z]+", question.lower())
    stop = {"do", "you", "what", "is", "are", "the", "your", "a", "an", "when",
            "how", "much", "many", "have", "already", "looking", "to", "for",
            "of", "in", "or", "and", "does", "did", "will", "would", "can"}
    keep = [w for w in words if w not in stop][:3]
    return "_".join(keep) if keep else f"answer_{index + 1}"


def brief_from_profile(name: str, industry: str, agent_type: str, language: str,
                       questions: list[str], profile: IndustryProfile,
                       success: str, agent_name: str | None,
                       lead_source: str = "") -> dict:
    """Turn researched facts + the client's questions into a brief."""
    objections = profile.industry_objections or []
    playbook = "\n".join(
        f"- {o.get('objection', '').strip()}\n"
        f"  they say: {o.get('spoken', '').strip()}\n"
        f"  reply: {o.get('handle', '').strip()}"
        for o in objections if o.get("objection")
    )
    proof = ("Use only real, verifiable examples of these kinds:\n"
             + "\n".join(f"- {p}" for p in profile.proof_types)
             + "\nIf you do not have a real one for their area, do not invent one."
             ) if profile.proof_types else (
             "Use only real, verifiable examples. Never invent one.")

    return {
        "business": name,
        "industry": industry,
        "agent_name": agent_name or DEFAULT_AGENT_NAME.get(language, "Priya"),
        "agent_type": agent_type,
        "language": language,
        "topic": industry,
        "lead_source": lead_source,
        "questions": [{"field": field_key(q, i), "ask": q}
                      for i, q in enumerate(questions)],
        "disqualify_if": profile.disqualifiers,
        "success": success,
        "products": (profile.what_we_offer or "").strip(),
        "customer_situation": (profile.customer_situation or "").strip(),
        "objection_playbook": playbook,
        "buying_signals": profile.buying_signals,
        "vocabulary": profile.customer_vocabulary,
        "proof": proof,
        "compliance": profile.compliance,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Build a voice agent from a brief.")
    ap.add_argument("--name", required=True, help="business name (spoken in the opening line)")
    ap.add_argument("--industry", required=True, help="what they sell, plain words")
    ap.add_argument("--type", dest="agent_type", default="outbound",
                    choices=["outbound", "inbound", "speed_to_lead",
                             "reactivation", "support"])
    ap.add_argument("--language", default="te-IN")
    ap.add_argument("--question", dest="questions", action="append", default=[],
                    help="repeatable; what the agent must find out")
    ap.add_argument("--success", default="", help="what a won call looks like")
    ap.add_argument("--lead-source", default="",
                    help="honest answer to 'how did you get my number' -- "
                         "required for outbound, never invented")
    ap.add_argument("--agent-name", default=None)
    ap.add_argument("--reuse-profile", action="store_true",
                    help="skip research if a profile already exists")
    args = ap.parse_args()

    slug = slugify(args.name)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = PROFILES_DIR / f"{slug}.json"

    print(f"  [1/3] researching {args.industry} ...", end=" ", flush=True)
    if args.reuse_profile and profile_path.exists():
        profile = IndustryProfile.load(profile_path)
        print("reused")
    else:
        profile = await research(args.name, args.industry,
                                 language=args.language,
                                 agent_type=args.agent_type,
                                 questions=args.questions)
        profile_path.write_text(profile.to_json(), encoding="utf-8")
        print(f"{len(profile.industry_objections)} industry objections, "
              f"{len(profile.compliance)} compliance rules")

    print("  [2/3] writing brief ......................", end=" ", flush=True)
    success = args.success or f"a confirmed next step for {args.industry}"
    brief_dict = brief_from_profile(args.name, args.industry, args.agent_type,
                                    args.language, args.questions, profile,
                                    success, args.agent_name,
                                    args.lead_source)
    brief_path = CLIENTS_DIR / f"{slug}.yaml"
    brief_path.write_text(
        yaml.safe_dump(brief_dict, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    print(brief_path.name)

    print("  [3/3] compiling prompt ...................", end=" ", flush=True)
    prompt = compile_prompt(Brief.from_yaml(brief_path))
    out = ROOT / ".tmp" / f"{slug}-prompt.md"
    out.write_text(prompt, encoding="utf-8")
    print(f"{len(prompt):,} chars, 0 nodes")

    print(f"\n  agent ready: {slug}")
    print(f"    brief    {brief_path.relative_to(ROOT)}")
    print(f"    profile  {profile_path.relative_to(ROOT)}")
    print(f"    prompt   {out.relative_to(ROOT)}")
    print(f"\n  next: python -m vaani.sim.run --brief {brief_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
