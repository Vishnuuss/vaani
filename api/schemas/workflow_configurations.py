from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from api.constants import (
    MAX_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
    MIN_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
    TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
)

DEFAULT_MAX_CALL_DURATION_SECONDS = 300
# Hard ceiling on configurable call duration. Must stay <= the concurrency
# rate limiter's stale_call_timeout (20 min): a call running past that has
# its slot purged as stale and the org concurrency limit under-counts.
MAX_CALL_DURATION_SECONDS = 1200
DEFAULT_MAX_USER_IDLE_TIMEOUT_SECONDS = 10.0
# MEASURED 2026-08-26: user_turn_secs was 0.803/0.802/0.806/0.815/0.816/0.816 s
# — dead constant, because it is VAD stop (0.2 s) + this value. It was 70% of
# total turn latency while LLM was 0.19 s and TTS 0.11 s. The semantic analyzer
# can end a turn earlier than this; the value is only its silence ceiling.
# Paired constraint (latency_budget.yaml): false-interruption rate must stay
# <= 2%. Telugu callers protested at 350 ms of TOTAL silence, and VAD's 0.2 s
# adds to this, so 0.2 here means 0.4 s total — above that complaint line.
DEFAULT_SMART_TURN_STOP_SECS = 0.2
DEFAULT_TURN_START_STRATEGY = "default"
DEFAULT_TURN_START_MIN_WORDS = 3
DEFAULT_PROVISIONAL_VAD_PAUSE_SECS = 1.5
DEFAULT_TURN_STOP_STRATEGY = "turn_analyzer"  # semantic, in-process, no network hop
# False = the semantic turn detector ends the turn; the transcript is
# bookkeeping and leaves the latency critical path (~438 ms/turn).
DEFAULT_TURN_WAIT_FOR_TRANSCRIPT = False
# OFF. Measured 0% hit rate over 9 real turns: qualification callers answer in
# two words, so there is never time for two partials to agree before the turn
# ends. It cost an extra LLM call per turn and returned nothing. Kept behind the
# flag rather than deleted, in case a long-utterance use case appears.
DEFAULT_SPECULATION_ENABLED = False
DEFAULT_CONTEXT_COMPACTION_ENABLED = False
# Race N identical completions per turn and speak whichever answers first.
# Measured on Groq gpt-oss-120b (bench/hedge.py, 2026-08-27): one request returns
# its first speakable token anywhere between 0.289 s and 1.450 s, and a caller
# experiences the turn they are in, not the median. Two concurrent copies cut p90
# from 1.132 s to 0.390 s, because the slow tail is a busy-worker effect that a
# second copy simply lands clear of. Set to 1 to disable.
# THREE, not two. Re-measured 2026-08-28 against the prompt that is actually
# live, which is larger than the one the original hedge=2 figure came from:
#
#     hedge-2   p50 0.653   p90 0.761
#     hedge-3   p50 0.325   p90 0.405
#
# -0.33s at the median. Run 217 shows why that is the number left to win: its
# one turn that came in at 0.715s had endpoint 0.361 AND llm 0.224, while every
# slower turn on the same call had an llm of 0.58-0.62. The endpoint is fixed;
# the LLM's spread is what stands between a good turn and a slow one.
#
# The losers are cancelled as soon as the winner produces content and the input
# is ~96% cached, so a third copy bills a few dozen reasoning tokens.
DEFAULT_LLM_HEDGE = 3
# How long the turn-stop strategy will wait for the STT's FINAL transcript,
# measured from true speech end.
#
# MEASURED 2026-08-27, run 93: `endpoint_secs` was 1.29-1.75s (median 1.42s) --
# 75% of a 1.9s turn. It is not VAD (0.2s) and not the model. pipecat ships
# `SARVAM_TTFS_P99 = 1.17` and the strategy waits `p99 - vad_stop_secs`, so every
# single turn paid Sarvam's NINETY-NINTH percentile finalisation latency. For
# comparison the same table lists Deepgram at 0.35 and Speechmatics at 0.74.
#
# A p99 exists so a transcript is essentially never truncated. That trade only
# makes sense if truncation is fatal, and here it is not: `PartialResponder`
# already promotes the newest partial when a turn ends before the final arrives.
# The safety net was built and then the deadline was set so late it never
# caught anything. Spending the p99 on every turn to protect a case that
# degrades gracefully is the wrong way round.
#
# 0.45s, lowered from 0.6s on 2026-08-28. This budget IS the slow path.
#
# Run 262's endpoint times are bimodal: 0.403s on the turns where the Telugu
# detector fired, and 0.78-0.93s clustered on the turns where it did not. That
# second cluster is 0.2s of VAD plus this budget, almost exactly -- so on the
# ~62% of turns the detector is unsure about, the caller waits out this constant
# and nothing else.
#
# Lowering it was chosen only after the alternatives were measured and rejected.
# `tools/sweep_turn_lead.py` retrained the detector to commit 0.1s, 0.2s and
# 0.3s earlier, which is what lowering `smart_turn_stop_secs` would also amount
# to. Recall collapsed faster than the time saved every time:
#
#     decide now       33.2% of turns end early    mean endpoint 0.718s
#     0.10s earlier    21.1%                       0.753s
#     0.20s earlier     5.5%                       0.837s
#
# Deciding sooner makes the call SLOWER on average. The budget is the only term
# left that can be cut without asking the model to judge on less evidence.
#
# 0.45s and not lower: Sarvam's measured p50 flush-to-final is 438ms
# (bench/FINDINGS §5), so this still catches about half the finals outright.
# The rest fall back to the newest partial, which `PartialResponder` already
# promotes -- and for the two-word answers these callers give, the newest
# partial is usually the entire answer. Going below the p50 would trade
# transcript accuracy for milliseconds, which is the wrong trade for a client
# who is complaining about quality.
DEFAULT_STT_FINALISATION_BUDGET_SECS = 0.45

# BACK ON, after the design was corrected. Read the history before changing it.
#
# It was switched off the night it shipped, on this evidence:
#
# The idea was sound: 0.92s of every gap is silence, so put a short "సరే" into
# it the moment the caller stops. The implementation fired the filler at the VAD
# stop, about 200ms after they finished -- and the real reply still took another
# second to arrive behind it.
#
# Run 267's bot track holds 30 audio bursts against 18 logged sentences. Twelve
# of the extras are fillers, and the gap between a filler and the sentence it was
# supposed to introduce is 1.0-1.8s:
#
#     36.80s  filler 0.17s        113.52s  filler 0.37s
#     37.80s  the sentence        115.34s  the sentence
#
# So the caller heard a stray word, then a second of silence, then the answer.
# That is worse than the silence alone: silence reads as thinking, while a word
# followed by silence reads as a machine that has lost its place. The client
# heard it immediately and described it as "aaa in the middle".
#
# The diagnosis named the fix, and the fix is now in place: a filler is not a
# marker dropped at the start of a gap, it is cover HELD until the gap ends.
# `filler_player.py` now streams, paced against a clock, and stops on the
# reply's first audio frame -- so the caller hears speech from ~0.2s after they
# finish, continuously, into the answer.
#
# On by default because the failure it replaces was a hole in the middle, and
# the tests now assert the caller never hears one (longest silence < 0.40s while
# covering). Set false per workflow to disable.
DEFAULT_FILLERS_ENABLED = True


class ExternalPBXFieldMapping(BaseModel):
    """Map one gathered-context value to a provider-native field."""

    context_path: str = Field(min_length=1, max_length=255)
    destination_field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

    @field_validator("context_path", mode="before")
    @classmethod
    def strip_context_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("destination_field", mode="before")
    @classmethod
    def strip_destination_field(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


# Extra lead fields to capture from the inbound INVITE, named without the
# provider's header prefix (``first_name`` -> ``X-VICIDIAL-first_name``). Each
# entry costs one ARI round trip during call setup, so the set is configured
# explicitly per workflow rather than enumerated off the INVITE.
MAX_EXTERNAL_PBX_LEAD_HEADERS = 50

ExternalPBXLeadHeader = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
]


class AmbientNoiseConfigurationDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    volume: float = 0.3


class WorkflowConfigurationDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _treat_null_as_unset(cls, data):
        # Stored configs (and older clients) carry explicit JSON nulls for
        # keys the user never configured; dropping them lets the field
        # defaults apply instead of failing validation.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data

    ambient_noise_configuration: AmbientNoiseConfigurationDefaults = Field(
        default_factory=AmbientNoiseConfigurationDefaults
    )
    max_call_duration: int = Field(
        default=DEFAULT_MAX_CALL_DURATION_SECONDS,
        gt=0,
        le=MAX_CALL_DURATION_SECONDS,
    )
    max_user_idle_timeout: float = DEFAULT_MAX_USER_IDLE_TIMEOUT_SECONDS
    smart_turn_stop_secs: float = DEFAULT_SMART_TURN_STOP_SECS
    turn_start_strategy: Literal["default", "min_words", "provisional_vad"] = (
        DEFAULT_TURN_START_STRATEGY
    )
    turn_start_min_words: int = DEFAULT_TURN_START_MIN_WORDS
    provisional_vad_pause_secs: float = DEFAULT_PROVISIONAL_VAD_PAUSE_SECS
    turn_stop_strategy: Literal["transcription", "turn_analyzer"] = (
        DEFAULT_TURN_STOP_STRATEGY
    )
    turn_wait_for_transcript: bool = DEFAULT_TURN_WAIT_FOR_TRANSCRIPT
    speculation_enabled: bool = DEFAULT_SPECULATION_ENABLED
    llm_hedge: int = Field(default=DEFAULT_LLM_HEDGE, ge=1, le=3)
    stt_finalisation_budget_secs: float = Field(
        default=DEFAULT_STT_FINALISATION_BUDGET_SECS, ge=0.2, le=3.0)
    dictionary: str = ""
    context_compaction_enabled: bool = DEFAULT_CONTEXT_COMPACTION_ENABLED
    text_chat_inactivity_timeout_seconds: int = Field(
        default=TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
        ge=MIN_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
        le=MAX_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
    )
    external_pbx_field_mappings: list[ExternalPBXFieldMapping] = Field(
        default_factory=list,
        max_length=100,
    )
    external_pbx_lead_headers: list[ExternalPBXLeadHeader] = Field(
        default_factory=list,
        max_length=MAX_EXTERNAL_PBX_LEAD_HEADERS,
    )

    @field_validator("external_pbx_lead_headers", mode="before")
    @classmethod
    def strip_lead_headers(cls, value: object) -> object:
        """Trim and de-duplicate while preserving the configured order."""
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        for item in value:
            name = item.strip() if isinstance(item, str) else item
            if name and name not in cleaned:
                cleaned.append(name)
        return cleaned


class TextChatInactivityTimeoutConstraints(BaseModel):
    """Backend-owned timeout metadata consumed by generated API clients."""

    default_seconds: int = TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS
    minimum_seconds: int = MIN_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS
    maximum_seconds: int = MAX_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS


def get_default_workflow_configurations() -> WorkflowConfigurationDefaults:
    return WorkflowConfigurationDefaults()
