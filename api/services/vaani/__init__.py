"""Vaani's brain, running on Dograh's infrastructure.

Dograh keeps what it is good at — campaigns, Redis, Postgres, telephony, the
dashboard. The *conversation* is Vaani's: one compiled system prompt, live
triage, a state block that keeps question coverage honest, and guardrails that
run before a single character reaches the speech engine.

    stt -> StateInjector -> aggregator.user() -> llm -> ReplyFilter -> tts

StateInjector sits BEFORE the aggregator so the state block is current when the
LLM fires. ReplyFilter sits AFTER the LLM so nothing unspeakable is ever spoken.
"""

from api.services.vaani.brain_processor import ReplyFilter, StateInjector
from api.services.vaani.compiler import Brief, compile_prompt
from api.services.vaani.state import CallState

__all__ = [
    "Brief",
    "CallState",
    "ReplyFilter",
    "StateInjector",
    "compile_prompt",
]
