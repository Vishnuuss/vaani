"""Industry research -- the step that makes onboarding a client one line long.

The promise: you type a business name, its industry, the agent type and a few
questions. Everything else -- what the offer actually is, how customers in that
industry push back, what you are legally not allowed to say, who is a waste of
time to sell to -- is researched here and compiled into Layer 3.

Nothing in this file is specific to any client or any industry. Layers 1, 2 and
4 (voice, psychology, mission) never change. This produces only Layer 3.

It runs OFF the call, once, at build time. Latency is irrelevant here, so it
uses the strongest model available rather than the fastest.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Offline job: pick for quality, not speed. gpt-oss-120b reasons well and is
# cheap on Groq; the realtime path uses a different, faster model entirely.
RESEARCH_BASE_URL = os.environ.get("VAANI_RESEARCH_BASE_URL",
                                   "https://api.groq.com/openai/v1")
RESEARCH_MODEL = os.environ.get("VAANI_RESEARCH_MODEL", "openai/gpt-oss-120b")
RESEARCH_API_KEY_ENV = os.environ.get("VAANI_RESEARCH_KEY_ENV", "GROQ_API_KEY")

LANGUAGE_NAMES = {
    "te-IN": "Telugu (as spoken in Telangana and Andhra Pradesh)",
    "hi-IN": "Hindi",
    "ta-IN": "Tamil",
    "kn-IN": "Kannada",
    "en-IN": "Indian English",
}

SYSTEM = """You are a sales researcher who briefs phone agents before they \
start calling for a new client.

You are given a business, its industry, and the market. Produce the factual and \
commercial ground truth an agent needs: what is actually being sold, how real \
customers in THIS industry push back, what cannot legally or ethically be \
claimed, and who is not worth selling to.

Rules that matter more than completeness:

- Write what is TRUE of the industry, not marketing copy. No superlatives.
- Never invent a price, a statistic, a customer name or a guarantee. If a number
  varies by customer, say that it varies.
- Objections must be the ones customers in this specific industry actually
  raise. Generic ones ("too expensive") are already handled elsewhere -- give
  the ones peculiar to THIS industry.
- Compliance lines must be real constraints for this industry in India.
- Reply with JSON only. No prose, no markdown fence."""

TEMPLATE = """Business name: {business}
Industry: {industry}
Market: India
Call language: {language}
Agent type: {agent_type}

The agent will ask the customer these questions:
{questions}

Return JSON with exactly these keys:

{{
  "what_we_offer": "3-4 plain sentences a phone agent could say. No adjectives \
that cannot be verified.",
  "customer_situation": "2-3 sentences on who the typical customer is and what \
problem brings them to this industry.",
  "industry_objections": [
    {{"objection": "in English",
      "spoken": "how a customer actually says it in {language}",
      "handle": "one or two sentences the agent should reply with, in \
{language}, using English words for any number"}}
  ],
  "disqualifiers": ["conditions that make this customer not worth selling to"],
  "compliance": ["things the agent must never say or promise in this industry \
in India"],
  "proof_types": ["kinds of proof that are credible in this industry -- \
categories, not invented examples"],
  "buying_signals": ["things a customer says in {language} that mean they are \
ready to commit"],
  "customer_vocabulary": ["the English loanwords real customers use for this \
industry when speaking {language}"]
}}

Give 5 to 7 industry_objections. Everything spoken must be natural {language} \
with English used for all numbers, prices, dates and times -- that is how \
educated speakers actually talk, and it is what the agent is required to do."""


@dataclass
class IndustryProfile:
    business: str
    industry: str
    language: str
    what_we_offer: str = ""
    customer_situation: str = ""
    industry_objections: list[dict] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    compliance: list[str] = field(default_factory=list)
    proof_types: list[str] = field(default_factory=list)
    buying_signals: list[str] = field(default_factory=list)
    customer_vocabulary: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str | Path) -> "IndustryProfile":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def _extract_json(text: str) -> dict[str, Any]:
    """Models wrap JSON in fences or prose no matter how firmly you ask."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in research reply: {text[:200]}")
    return json.loads(text[start:end + 1])


async def research(business: str, industry: str, *, language: str = "te-IN",
                   agent_type: str = "outbound",
                   questions: list[str] | None = None,
                   timeout: float = 180.0) -> IndustryProfile:
    """Ask the model for the industry's ground truth. One call, off the hot path."""
    key = os.environ.get(RESEARCH_API_KEY_ENV)
    if not key:
        raise RuntimeError(f"{RESEARCH_API_KEY_ENV} not set")

    lang_name = LANGUAGE_NAMES.get(language, language)
    qs = "\n".join(f"- {q}" for q in (questions or [])) or "- (none given yet)"
    prompt = TEMPLATE.format(business=business, industry=industry,
                             language=lang_name, agent_type=agent_type,
                             questions=qs)

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{RESEARCH_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": RESEARCH_MODEL,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 4000,
            },
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]

    data = _extract_json(content)
    known = set(IndustryProfile.__dataclass_fields__)
    data = {k: v for k, v in data.items() if k in known}
    return IndustryProfile(business=business, industry=industry,
                           language=language, **data)
