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

# --- Two-sided endpointing -------------------------------------------------
# `smart_turn_stop_secs` above is a SINGLE number: every caller gets the same
# silence window whatever they just said. Run 295 is what that costs. The
# caller said, in order:
#
#   "ఇంకా చెప్పలే కదా బిల్లు"        I haven't even told you the bill yet
#   "అప్పుడే మీరు క్వశ్చన్"          you're already on the next question
#   "అసలు మిమ్మల్ని ఆన్సర్ చెప్పనియ్యరా మీరు?"  do you even let me answer?
#
# then hung up. VAD stop is 0.2 s and this was 0.2 s, so nobody on that call
# was ever allowed to pause for more than about 0.4 s -- and "60 ... aaa ... 70"
# needs more than that. The turn was cut at "60".
#
# The fix is the one every production stack uses: make the wait a FUNCTION of
# how finished the caller sounds. LiveKit ships `min_endpointing_delay` 0.5 s
# and `max_endpointing_delay` 6.0 s -- confident the turn ended, wait `min`;
# unsure, wait up to `max`. Vaani already has the probability (TeluguTurnAnalyzer
# scores the tail of every utterance) and has only ever been allowed to spend it
# on ending turns EARLIER. These two numbers let it also hold one open.
#
# 6.0 s is right for LiveKit's general case and wrong for a sales call, where a
# caller who has genuinely stopped must not sit in silence. 1.4 s here is
# 1.6 s of total silence with VAD, which is roughly the longest hesitation
# measured in the harvested caller recordings.
# The wait when the model has scored the turn but NOT above its bar -- i.e. it
# has an opinion and the opinion is "probably not finished".
#
# Raised from 0.05 on 7 Sep. Measured over 589 recorded speech bursts:
#
#     0.05 (was)   32.6% cut off   wait p50 0.30s   p90 0.50s
#     0.70         27.7%           wait p50 0.30s   p90 1.00s
#     0.70         27.7%           wait p50 0.30s   p90 1.00s
#     0.90         26.5%           wait p50 0.30s   p90 1.18s
#
# With the 250ms blind floor beside it, 0.70 measures 24.4% and 0.90 measures
# 22.9%. 0.70 is chosen for 1.5 points more cut-offs because it holds the
# analyzer's p90 at exactly 1.00s, which is the client's stated ceiling for a
# whole reply. Buying the last points by exceeding the ceiling he set is not
# mine to do.
#
# The MEDIAN DOES NOT MOVE. A turn the model is confident about ends on the
# immediate path and never reaches this value, so the typical turn is exactly as
# fast as before; the extra patience lands only on turns the model already
# doubts, which is where the caller is still talking. That is the whole point --
# every earlier attempt at this bought cut-offs with latency on EVERY turn.
#
DEFAULT_ENDPOINT_MIN_SECS = 0.70
DEFAULT_ENDPOINT_MAX_SECS = 1.40     # + VAD 0.2 = 1.60 s when clearly mid-thought
# A short utterance is where the prosody model is least reliable and where being
# wrong is most obvious ("మాది." is both a complete answer and the first word of
# a sentence). Short turns therefore never end on less than this, whatever the
# interpolation says -- the early COMPLETE path can still fire on them and does.
# How long the turn is held open once the TEXT says the sentence is unfinished.
#
# Raised from 0.45 on 6 Sep. Priced on 589 recorded speech bursts against a
# perfect completeness signal: at 0.45 the cut-off rate is 18.0%, at 0.70 it is
# 10.4% and at 1.00 it is 7.5% -- and the wait does not move at all (p50 0.30s,
# p90 0.48s, better than the 0.50s it ships with today).
#
# It is free because the hold applies ONLY to turns the text has flagged as
# unfinished, and on those the caller is still speaking -- so it is not a wait,
# it is cancelled by his own voice. Turns that really have ended never reach it.
#
# On today's grammar rules, which flag only 5.5% of transcripts, the measured
# gain is small (33.3% -> 32.6%) and the measured cost is zero. It is raised now
# because it is the ceiling a better completeness signal would be spending, and
# leaving it at 0.45 would cap that work at 18%.
DEFAULT_ENDPOINT_FRAGMENT_FLOOR_SECS = 1.00

# ---------------------------------------------------------------------------
# The rest of the turn detector's timings.
#
# These existed only as `TeluguTurnParams` defaults and module constants in
# `telugu_turn.py`, and `turn_taking.py` forwarded four of them out of fourteen
# -- so changing any of the others meant editing Python and redeploying the
# voice container. The client's objection on 7 Sep was exactly this, and it was
# correct:
#
#     "hardcoding the turn detection to 450ms without understanding intent is
#      not good"
#
# The number is not the problem; a number baked into the source is. How long to
# wait before deciding a person has stopped talking is a per-agent, per-language
# judgement -- a Telugu caller assembling "అరవై ... డెబ్భై అనుకుంటా" and an
# English caller saying "yes" do not deserve the same stopwatch -- and it cannot
# be a judgement while it needs a deploy.
#
# Every default below is the value that was already hardcoded, so behaviour is
# UNCHANGED until somebody deliberately sets one. This moves the dial to
# somewhere it can be turned; it does not turn it.
# ---------------------------------------------------------------------------

# Floor for turns the model is NOT nearly certain about, whatever their length.
# The fix for being cut off mid-answer: a caller five seconds into an
# explanation used to get no floor at all, because the fragment floor is gated
# on the turn being SHORT.
DEFAULT_ENDPOINT_UNSURE_FLOOR_SECS = 0.30
# How close to the trained threshold still counts as "nearly certain". At 0.95
# the median turn takes the fast path untouched, which is what makes the floor
# above cost nothing on calls that already work.
DEFAULT_ENDPOINT_UNSURE_BAND = 0.95
# The least silence required before the CONFIDENT path may end a turn while the
# analyzer holds no transcript for what the caller just said. Measured 6 Sep
# over 589 speech bursts: the text for the CURRENT burst has arrived only 31.2%
# of the time when this decision is taken; 24.1% of decisions see no text at all.
DEFAULT_BLIND_MIN_SILENCE_MS = 250.0
# The same floor for a SHORT utterance with no transcript yet -- where a filler
# like "ఆ" lives. Sized to outlast Sarvam's ~0.35 s so the words actually arrive
# and the text half of the decision can be used. THIS is the 450 the client
# objected to; it is a default now, not a constant.
DEFAULT_BLIND_SHORT_SILENCE_MS = 450.0
# Below this a turn is treated as a fragment rather than an answer, and needs
# near-certainty before it may end a turn.
DEFAULT_TURN_FRAGMENT_SECS = 0.65
# The near-certainty demanded of those fragments.
DEFAULT_TURN_SHORT_THRESHOLD = 0.995
# How much silence must accumulate before the model is asked at all.
DEFAULT_TURN_MIN_SILENCE_MS = 120.0
# How much of the utterance's tail the model reads.
DEFAULT_TURN_WINDOW_SECS = 1.5
# If the caller starts again within this, we did not end his turn -- we
# interrupted it.
DEFAULT_TURN_RESUME_WINDOW_SECS = 1.0
# How many such interruptions before this caller is treated as one the model
# reads badly and given the floor on every turn. Two, not one: one is a cough,
# two is a pattern.
DEFAULT_TURN_CUTOFFS_BEFORE_ADAPTING = 2

# WHICH trained artifact scores the turn.
#
# Two exist, both 16-feature, both trained on `turnstops_real.jsonl` (2,950
# rows, 1,707 real turn ends / 1,243 real interruptions, split by CALL):
#
#   "forest"  audio_turn_gbm.json      250 trees. What production has run since
#                                      28 Aug. Measured 7 Sep on real negatives:
#                                      6.45% false cutoffs, 12.38% endable early.
#   "linear"  audio_turn_weights.json  logistic. 2.05% / 12.62% at threshold
#                                      0.70 -- it dominates the forest on BOTH
#                                      halves of the promotion rule.
#
# Until 7 Sep this was not a choice: `TeluguTurnAnalyzer.__init__` did
# `_load_forest(...) or _load(...)`, so a forest on disk always won and the
# linear weights were unreachable dead code. Shipping a better linear model
# meant DELETING the forest file. That is not a deploy, it is a demolition --
# and it is not revertible from config, which is exactly the property a live
# client agent needs most.
#
#   "timer"   no model at all. The endpoint floors decide alone. Measured
#             7 Sep on 60 recorded calls: a model-free wait beats every
#             trained model at equal patience, so this has to be
#             expressible before it can be tested on a live call.
#
# "forest" is the default because it is what runs today. Changing this changes
# behaviour; nothing changes until someone sets it deliberately.
DEFAULT_TURN_MODEL = "forest"

DEFAULT_TURN_START_STRATEGY = "default"
DEFAULT_TURN_START_MIN_WORDS = 3
DEFAULT_PROVISIONAL_VAD_PAUSE_SECS = 1.5
DEFAULT_TURN_STOP_STRATEGY = "turn_analyzer"  # semantic, in-process, no network hop
# False = the semantic turn detector ends the turn; the transcript is
# bookkeeping and leaves the latency critical path (~438 ms/turn).
DEFAULT_TURN_WAIT_FOR_TRANSCRIPT = False
# ON, after the reason for the 0% hit rate turned out to be a bug in the trigger
# rather than a fact about the traffic.
#
# The old note read: "Measured 0% hit rate over 9 real turns: qualification
# callers answer in two words, so there is never time for two partials to agree
# before the turn ends." The first half was measured. The second half was the
# wrong conclusion drawn from it.
#
# The trigger fired on the common prefix of the last TWO partials, which cannot
# contain the newest word:
#
#     partial "వన్"              fires on nothing
#     partial "వన్ లాక్"          fires on "వన్"
#     partial "వన్ లాక్ అండి"     fires on "వన్ లాక్"
#     final   "వన్ లాక్ అండి"     MISS -- one word behind, always
#
# 0% was structural. It fired on the newest partial and the same sequences hit.
#
# WHY THIS CANNOT INTERRUPT, which is the objection that matters. Speculation
# changes when the agent THINKS, never when it SPEAKS. The coordinator hands
# tokens over only through `take_response_for`, which the gate calls only on an
# LLM trigger frame -- and that frame arrives only after the turn detector has
# ended the turn. A wrong guess is cancelled and cannot reach the caller. The
# turn detector keeps sole authority over when the agent opens its mouth.
#
# WHAT WENT WRONG LAST TIME. Run 92 lost a call entirely -- zero pipeline
# events. The probe passed frames through untouched but issued REAL generations,
# and with hedging already sending three requests per turn, firing again on
# every partial multiplies concurrent load on the provider. Contention, not
# logic. So speculation is now capped at 2 generations per turn and skips
# one-word partials.
#
# Expected: the LLM's 0.279s p50 moves inside the 0.758s endpoint window it is
# already spending, taking the total from ~1.10s toward ~0.82s -- on the turns
# that hit. Misses cost tokens and nothing else.
DEFAULT_SPECULATION_ENABLED = True
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
# "it should speak while generating only not after complete" -- the client,
# 5 Sep. Cartesia aggregates whole SENTENCES before synthesising by default, and
# `SimpleTextAggregator` will not release one until a non-whitespace character
# arrives after its terminal punctuation. A Vaani reply is usually a single
# sentence ending in a question mark, so that character never comes and the TTS
# receives nothing until the LLM has finished the entire reply. pipecat's own
# Cartesia docstring puts the generic cost at "~200-300ms per sentence".
#
# Shipped off, listened to, and now ON by default.
#
# The client made one real call on wf2 with it enabled -- run 776, 14 turns --
# and confirmed it sounds right. Measured against run 392 on the same agent
# before it:
#
#     p50 TOTAL   1.096s -> 0.877s
#     LLM         0.527s -> 0.351s
#     TTS first   0.069s -> 0.106s
#
# TTS first-audio RISING while TOTAL falls is the signature of the change
# working: the number now covers a short opening chunk instead of being clocked
# from a whole buffered sentence, so it is measuring less of the turn.
#
# Default True so every agent built from here gets it without anyone
# remembering to set it.
# Let the LLM decide whether the caller has finished, instead of a timer.
#
# The client, 5 Sep, after a live call: "you need to understand intent by
# semantic naa whether sentence is complete or not". Run 790 is why:
#
#     USER : ...ఇండస్ట్రియల్ ఏరియాలో ఉంటాను.
#     USER : సిటీకి కొంచెం బయట.          <- still talking
#     BOT  : సరే, మీకు సొంత              <- cut in
#
# "ఇండస్ట్రియల్ ఏరియాలో ఉంటాను" is a grammatically complete sentence. The
# prosody model hears a falling contour; `completeness.sounds_unfinished` finds
# no dangling quantity, no open range, no connective, no hesitation. Both
# signals say finished and both are wrong, because grammatically complete is
# not conversationally complete -- and no timer and no grammar rule can tell
# those apart.
#
# OFF by default: it changes the reply FORMAT (a marker precedes the MODE
# line), so it is switched on per workflow and listened to before it spreads.
DEFAULT_SEMANTIC_TURN_COMPLETION = False

DEFAULT_TTS_TOKEN_STREAMING = True

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
# OFF, and staying off. Two designs, two live failures, both audible to the
# client on the first call he made.
#
#   v1, run 267:  one clip at the VAD stop, then 1.0-1.8s of silence before the
#                 reply. "that aaa in the middle ... it is like scripted"
#   v2, run 273:  continuous paced cover, stopping on the reply's first audio.
#                 The caller heard the cover as a stray word and asked what it
#                 was: "ఆ ఏంది మంచిది ఏంది అది?" -- what is this "మంచిది"?
#
# v2 fixed the hole and the fix worked; the tests still hold. The defect it
# exposed is upstream of the timing entirely: a pre-recorded word played from a
# clip does not sound like the sentence it precedes. It has different prosody,
# it does not lead anywhere, and a caller notices a voice saying "మంచిది" at
# them for no reason. Covering a gap convincingly needs speech generated as part
# of the reply, not spliced in front of it -- which is a TTS-level change, not a
# playback one.
#
# The machinery is kept because the measurement behind it stands: 0.72s of every
# gap is silence and a human agent fills it. But it is not going back on a live
# client call on a hunch. Enable per workflow only with a way to hear it first.
DEFAULT_FILLERS_ENABLED = False


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
    endpoint_min_secs: float = Field(
        default=DEFAULT_ENDPOINT_MIN_SECS, ge=0.0, le=2.0)
    endpoint_max_secs: float = Field(
        default=DEFAULT_ENDPOINT_MAX_SECS, ge=0.1, le=5.0)
    endpoint_fragment_floor_secs: float = Field(
        default=DEFAULT_ENDPOINT_FRAGMENT_FLOOR_SECS, ge=0.0, le=3.0)
    # The rest of the endpoint window. Bounds are deliberately generous at the
    # top: an agent whose callers think in long pauses should be able to say so
    # without a deploy. `le` exists to catch a typo, not to express taste.
    endpoint_unsure_floor_secs: float = Field(
        default=DEFAULT_ENDPOINT_UNSURE_FLOOR_SECS, ge=0.0, le=3.0)
    endpoint_unsure_band: float = Field(
        default=DEFAULT_ENDPOINT_UNSURE_BAND, ge=0.0, le=1.0)
    blind_min_silence_ms: float = Field(
        default=DEFAULT_BLIND_MIN_SILENCE_MS, ge=0.0, le=2000.0)
    blind_short_silence_ms: float = Field(
        default=DEFAULT_BLIND_SHORT_SILENCE_MS, ge=0.0, le=2000.0)
    turn_fragment_secs: float = Field(
        default=DEFAULT_TURN_FRAGMENT_SECS, ge=0.0, le=3.0)
    turn_short_threshold: float = Field(
        default=DEFAULT_TURN_SHORT_THRESHOLD, ge=0.0, le=1.0)
    turn_min_silence_ms: float = Field(
        default=DEFAULT_TURN_MIN_SILENCE_MS, ge=0.0, le=1000.0)
    turn_window_secs: float = Field(
        default=DEFAULT_TURN_WINDOW_SECS, ge=0.2, le=5.0)
    turn_resume_window_secs: float = Field(
        default=DEFAULT_TURN_RESUME_WINDOW_SECS, ge=0.0, le=5.0)
    turn_cutoffs_before_adapting: int = Field(
        default=DEFAULT_TURN_CUTOFFS_BEFORE_ADAPTING, ge=1, le=20)
    turn_model: Literal["forest", "linear", "timer"] = DEFAULT_TURN_MODEL
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
    tts_token_streaming: bool = DEFAULT_TTS_TOKEN_STREAMING
    semantic_turn_completion: bool = DEFAULT_SEMANTIC_TURN_COMPLETION
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
