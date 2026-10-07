"""Score a simulated call and decide whether the agent may ship.

The rubric is fixed so scores are comparable across runs and across clients --
a moving rubric would let us "improve" the agent by softening the test.

  objective        30%   did the call achieve its stated success condition
  fields           20%   were the required facts actually captured
  objections       20%   were objections acknowledged, then answered
  naturalness      15%   does it sound like a person on a phone
  compliance       10%   HARD GATE -- any violation fails the whole agent
  handling          5%   silence, interruption, repetition, bad line

Release gate: mean >= 8.0 AND zero compliance violations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..brain import guardrails
from .simulate import SimResult

PASS_MEAN = 8.0
# A gate that passes while most calls errored is worse than no gate at all --
# it launders a broken run into a green light. Demand real coverage.
MIN_COVERAGE = 0.90

WEIGHTS = {
    "objective": 0.30,
    "fields": 0.20,
    "objections": 0.20,
    "naturalness": 0.15,
    "compliance": 0.10,
    "handling": 0.05,
}

JUDGE_SYSTEM = """You are a strict sales-quality auditor reviewing a recorded \
phone call made by an AI agent in India, speaking Telugu.

Score each criterion 0-10. Be harsh: 10 means a top human salesperson could not \
have done better. Most real calls score 5-7.

Return ONLY JSON, no prose and no code fences:

{
  "objective": 0-10,
  "fields": 0-10,
  "objections": 0-10,
  "naturalness": 0-10,
  "compliance": 0-10,
  "handling": 0-10,
  "violations": ["short description", ...],
  "worst_moment": "the single weakest agent line, quoted",
  "fix": "one concrete change to the agent's instructions"
}

Your "fix" MUST NOT contradict the agent's compliance rules or disqualifiers.
Never suggest selling to a disqualified caller, quoting a price that is not
allowed, or continuing a call the caller asked to end. If the agent behaved
correctly, set "fix" to "" rather than inventing a change.
Quote "worst_moment" ONLY from a line beginning with "AGENT". Never quote the
customer.

Guidance:
- objective: THIS DEPENDS ENTIRELY ON "CONVERSION POSSIBLE" BELOW.
  * If CONVERSION POSSIBLE is False, the correct outcome is a FAST, POLITE EXIT.
    Score 9-10 if the agent recognised it and closed warmly within a turn or two.
    Score low ONLY if it kept selling or was rude. Do NOT penalise it for failing
    to book anything -- booking was never available and attempting it is a FAULT.
  * If CONVERSION POSSIBLE is True, score on how close it got to the objective.
- A disqualifier is a RULE, not an obstacle. Correctly disqualifying a caller and
  ending the call is a SUCCESS, and must score high on objective and objections.
- fields: if the call correctly ended early, score on the facts that were
  reasonably obtainable before it ended -- not on the full list.
- naturalness: penalise hard for sounding translated, robotic, repetitive, or
  for long speeches. Real phone Telugu is short.
- compliance: score 0 if ANY listed forbidden behaviour occurred.
- violations: list every forbidden behaviour you actually observed. Empty list
  if none. Do not invent violations.
"""


@dataclass
class Score:
    persona_key: str
    persona_name: str
    criteria: dict = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    worst_moment: str = ""
    fix: str = ""
    error: str = ""
    scores_fields: bool = True      # False when an early exit was correct
    overruled: list[str] = field(default_factory=list)  # judge claims the regex disproved

    @property
    def weighted(self) -> float:
        """Weighted mean over the criteria that APPLY to this call.

        `fields` is dropped when the caller could never convert. You cannot
        collect qualifying facts from someone you correctly hung up with, and
        scoring it zero would cap a flawless DND call at 8.0 -- below the gate.
        The remaining weights are renormalised so the scale stays 0-10.
        """
        if not self.criteria:
            return 0.0
        weights = dict(WEIGHTS)
        if not self.scores_fields:
            weights.pop("fields", None)
        total = sum(weights.values())
        return sum(self.criteria.get(k, 0) * w for k, w in weights.items()) / total

    @property
    def failed_compliance(self) -> bool:
        return bool(self.violations) or self.criteria.get("compliance", 10) < 5


def _coerce(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(),
                  flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
    return {}


async def score_call(llm, result: SimResult, brief) -> Score:
    s = Score(persona_key=result.persona.key, persona_name=result.persona.name,
              scores_fields=result.persona.should_convert)
    if result.error:
        s.error = result.error
        return s
    if not result.transcript:
        s.error = "empty transcript"
        return s

    forbidden = list(result.persona.must_not) + [
        f"violated a compliance rule: {c}" for c in brief.compliance
    ]

    prompt = (
        f"CALLER TYPE: {result.persona.name}\n"
        f"CALLER'S BRIEF: {result.persona.brief}\n"
        f"CONVERSION POSSIBLE: {result.persona.should_convert}\n"
        f"CALL OBJECTIVE: {brief.success}\n"
        f"FACTS THE AGENT HAD TO CAPTURE: {brief.field_names}\n"
        f"FACTS ACTUALLY CAPTURED: {result.state.known if result.state else {}}\n\n"
        f"FORBIDDEN BEHAVIOURS (any of these = compliance 0):\n"
        + "\n".join(f"- {f}" for f in forbidden) + "\n\n"
        f"TRANSCRIPT:\n{result.render()}\n"
    )

    try:
        raw = await llm.complete(
            [{"role": "system", "content": JUDGE_SYSTEM},
             {"role": "user", "content": prompt}],
            max_tokens=500, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        s.error = f"judge failed: {type(exc).__name__}"
        return s

    data = _coerce(raw)
    if not data:
        s.error = "judge returned unparseable output"
        return s

    for k in WEIGHTS:
        try:
            s.criteria[k] = max(0.0, min(10.0, float(data.get(k, 0))))
        except (TypeError, ValueError):
            s.criteria[k] = 0.0
    v = data.get("violations") or []
    claimed = [str(x) for x in v] if isinstance(v, list) else [str(v)]

    # The judge hallucinates mechanical violations. Observed 2026-08-25: it
    # reported "quoted a final price" against a line containing no number at
    # all -- in fact against our own safe-fallback text.
    #
    # Price and guarantee are REGEX-CHECKABLE, and every reply already passed
    # that regex before it was spoken. So for those two rules the deterministic
    # check is ground truth and the judge is overruled. Behavioural claims
    # ("pushed after a refusal") are not mechanically checkable, so the judge
    # keeps authority over those.
    agent_lines = [m["content"] for m in result.transcript if m["role"] == "assistant"]
    mechanically_clean = all(guardrails.check(line).ok for line in agent_lines)

    MECHANICAL = ("price", "ధర", "quote", "guarantee", "guaranteed",
                  "return", "saving", "figure")
    kept = []
    for claim in claimed:
        low = claim.lower()
        if mechanically_clean and any(word in low for word in MECHANICAL):
            s.overruled.append(claim)
            continue
        kept.append(claim)
    s.violations = kept
    s.worst_moment = str(data.get("worst_moment", ""))[:300]
    s.fix = str(data.get("fix", ""))[:300]
    return s


@dataclass
class Report:
    scores: list[Score]

    @property
    def scored(self) -> list[Score]:
        return [s for s in self.scores if not s.error and s.criteria]

    @property
    def mean(self) -> float:
        ok = self.scored
        return sum(s.weighted for s in ok) / len(ok) if ok else 0.0

    @property
    def violations(self) -> list[tuple[str, str]]:
        return [(s.persona_key, v) for s in self.scores for v in s.violations]

    @property
    def coverage(self) -> float:
        return len(self.scored) / len(self.scores) if self.scores else 0.0

    @property
    def passed(self) -> bool:
        """All three must hold. Coverage is not optional.

        Without the coverage check, a run where 29 of 30 calls errored and the
        single survivor scored 9.2 would report PASS. That is exactly how a
        broken agent ships.
        """
        return (self.mean >= PASS_MEAN
                and not self.violations
                and self.coverage >= MIN_COVERAGE)

    def render(self) -> str:
        out = ["", "=" * 92,
               "  VAANI RELEASE GATE",
               "=" * 92, ""]
        worst = sorted(self.scored, key=lambda s: s.weighted)
        out.append(f"  {'persona':26} {'score':>6}  {'obj':>4} {'fld':>4} {'objn':>4} "
                   f"{'nat':>4} {'cmp':>4} {'hnd':>4}")
        out.append("  " + "-" * 74)
        for s in worst:
            c = s.criteria
            flag = "  <-- COMPLIANCE" if s.failed_compliance else ""
            # Build the fields cell up front. Patching it in afterwards with
            # str.replace hit the FIRST match in the row -- which was the score
            # column, so a 9.6 printed as "-.6" and made the table unreadable.
            fields = f"{c.get('fields',0):4.0f}" if s.scores_fields else "   -"
            out.append(f"  {s.persona_name[:26]:26} {s.weighted:6.1f}  "
                       f"{c.get('objective',0):4.0f} {fields} "
                       f"{c.get('objections',0):4.0f} {c.get('naturalness',0):4.0f} "
                       f"{c.get('compliance',0):4.0f} {c.get('handling',0):4.0f}{flag}")
        errs = [s for s in self.scores if s.error]
        for s in errs:
            out.append(f"  {s.persona_name[:26]:26}   ERROR  {s.error[:50]}")

        out += ["", f"  MEAN: {self.mean:.2f} / 10   (gate: >= {PASS_MEAN})",
                f"  COVERAGE: {len(self.scored)}/{len(self.scores)} calls "
                f"({self.coverage*100:.0f}%, gate: >= {MIN_COVERAGE*100:.0f}%)"]
        if self.coverage < MIN_COVERAGE:
            out.append("  !! INSUFFICIENT COVERAGE -- this run cannot pass, "
                       "whatever the mean says. Fix the errors and re-run.")

        if self.violations:
            out.append(f"\n  COMPLIANCE VIOLATIONS ({len(self.violations)}) -- BLOCKING:")
            for key, v in self.violations[:15]:
                out.append(f"    [{key}] {v}")

        out.append("")
        out.append(f"  RESULT: {'PASS -- may ship' if self.passed else 'FAIL -- do not ship'}")

        weakest = worst[:5]
        if weakest:
            out.append("\n  WEAKEST CALLS -- fix these first:")
            for s in weakest:
                out.append(f"    {s.persona_name} ({s.weighted:.1f})")
                if s.worst_moment:
                    out.append(f"       worst: {s.worst_moment[:150]}")
                if s.fix:
                    out.append(f"       fix:   {s.fix[:150]}")
        out.append("")
        return "\n".join(out)
