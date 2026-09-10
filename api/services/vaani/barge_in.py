"""Vaani owns barge-in: WHICH caller sounds are allowed to stop the bot.

The defect this exists for
--------------------------
Every user-turn start broadcasts an interruption, unconditionally. In
`pipecat/src/pipecat/processors/aggregators/llm_response_universal.py`::

    if params.enable_interruptions:
        await self.broadcast_interruption()

and `enable_interruptions` defaults to True on every start strategy. There is
no minimum duration, no minimum energy and no confidence of any kind between
"the caller made a noise" and "stop the bot mid-word".

On the live MB Solar workflow the stored config is::

    turn_start_strategy: min_words
    turn_start_min_words: 1

so `MinWordsUserTurnStartStrategy` fires on the FIRST interim transcript
containing one word. One filler syllable cuts the bot off.

That is wrong for Telugu specifically. Telugu callers backchannel constantly
while they are listening -- "ఆ", "సరే", "అవును", "హా". These are the caller
saying *go on*, not the caller taking the floor. Cutting the bot off on them
is the loudest complaint on this agent.

But the opposite error is worse. "ఆగండి" (wait), "ఆపండి" (stop), "వద్దు" (no),
"ఒక నిమిషం" (one minute), "కాదు" (not that) MUST stop the bot instantly, and a
caller who genuinely starts a sentence over the bot must win the floor. A gate
that is unsure has to let the interruption through.

Where the gate sits, and why there
----------------------------------
`enable_interruptions` is already the exact lever: it is carried per-turn in
`UserTurnStartedParams`, and the aggregator reads it at the one line above. So
this module does not add a new mechanism -- it makes that existing flag a
DECISION instead of a constant.

`BargeInGatedUserTurnStartStrategy` wraps whatever start strategy the workflow
configured. It forwards every frame to the inner strategy unchanged, so the
inner strategy's detection is untouched; it intercepts only the inner
strategy's `on_user_turn_started` event and, when the gate declines, re-emits
it with `enable_interruptions=False`.

That distinction matters and is the reason this is not implemented as
"swallow the turn start". The user turn STILL starts when the gate declines:
the caller's words are still aggregated, still transcribed, still answered.
The only thing suppressed is stopping the bot's current sentence. Swallowing
the turn would lose the caller's speech, which is a much worse failure than
the one being fixed.

Everything here is synchronous and arithmetic. No LLM call, no network, no
model load -- it runs inside the frame path on every audio frame.

Fail-open, always
-----------------
`should_interrupt` is wrapped in a try/except that returns True. A bug in this
file must never be able to make the bot un-interruptible, which is far worse
than the barge-in it was meant to prevent. `BaseObject._run_handler` swallows
handler exceptions, so an unguarded raise here would fail SILENTLY.

Off by default
--------------
`barge_in_gate_enabled` defaults to False and `apply_barge_in_gate` then
returns the caller's list unchanged -- the same objects, not copies. Nothing
about today's behaviour changes until someone sets that key deliberately.
"""

from __future__ import annotations

import re
import time
from array import array
from dataclasses import dataclass

from loguru import logger

from api.schemas.workflow_configurations import (
    DEFAULT_BARGE_IN_GATE_ENABLED,
    DEFAULT_BARGE_IN_LEXICAL,
    DEFAULT_BARGE_IN_MAX_BACKCHANNEL_WORDS,
    DEFAULT_BARGE_IN_MIN_RMS_RATIO,
    DEFAULT_BARGE_IN_MIN_SPEECH_SECS,
)
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
    UserTurnStartedParams,
)

# --- Lexicon ---------------------------------------------------------------
#
# Deliberately SHORT. A word only belongs in BACKCHANNELS if it can never, on
# its own, be a caller trying to take the floor. "అవును" (yes) qualifies:
# alone it is an acknowledgement, and if the caller keeps going the duration
# and word-count checks release the gate a moment later anyway. "వద్దు" (no)
# does not qualify -- alone it is a refusal and has to land immediately.
#
# The phrases are matched as substrings because STT splits them
# inconsistently ("ఒక నిమిషం" / "ఒక్క నిమిషం" / "ఒకనిమిషం").

BACKCHANNELS = frozenset({
    # Telugu
    "ఆ", "ఆఁ", "అ", "అఁ", "హా", "హ", "హు", "హ్మ్", "ఊ", "ఉ", "ఊఁ",
    "సరే", "సరేనండి", "సరేనం", "అవును", "ఔను", "ఓకే", "అలాగే", "మ్మ్",
    "అండి", "సార్",
    # --- added 10 Sep, from `tools/mine_backchannels.py` ------------------
    #
    # Every one of these is a TRANSCRIPTION VARIANT of a sound already in the
    # set above, not a new word, and that distinction is the entire safety
    # argument. The miner read all 2,097 run logs and found only 106 distinct
    # one-word caller utterances; the frequent ones that are NOT already here
    # are all CONTENT -- "సొంతమే" (it's owned) 79, "చెప్పండి" (tell me) 102,
    # "లేదు" (no) 17 -- and each is said in answer to a question 85-100% of the
    # time. Adding a content word here would make the agent deaf to an answer.
    # So the set grows sideways only, into spellings of sounds already accepted.
    "ఆహా", "ఆఁహా", "ఊం", "ఆం", "అం", "హూ", "హుఁ", "హ్మ్మ్", "మ్",
    "ఉమ్", "సరేనండీ", "సరేలే", "ఓకె", "అలాగేనండి", "ఆఁఆఁ", "అవునౌను",
    # Latin-script spellings Sarvam/Deepgram emit for the same sounds
    "aa", "haa", "ha", "hmm", "hm", "mhm", "mm", "uh", "huh",
    "sare", "avunu", "ok", "okay", "yes", "yeah", "yep", "right",
    "aan", "aam", "hoon", "ho", "acha", "achha", "sari", "saree",
    "umm", "um", "mhmm", "uhuh", "yup", "sure", "correct", "fine",
})

# Anything here stops the bot instantly, whatever duration or energy say.
INTERRUPT_WORDS = frozenset({
    "ఆగండి", "ఆగు", "ఆగండీ", "ఆపండి", "ఆపు", "ఆపండీ",
    "వద్దు", "వద్దండి", "వొద్దు", "కాదు", "కాదండి",
    "వినండి", "చెప్పనివ్వండి", "ఉండండి", "ఉండు",
    "stop", "wait", "no", "hold",
    # --- added 10 Sep, and this is the half the hand-written list missed ---
    #
    # The miner surfaced a class that was nowhere in either lexicon: the
    # caller signalling that the CHANNEL has failed. "హలో" is the single most
    # common thing anyone says to this agent -- 381 times, three times more
    # than the next -- and a caller saying "hello?" while the bot is talking is
    # a caller who cannot hear it. Continuing to talk over them is the worst
    # possible response. Same for the repair words: "ఏమన్నారు" (what did you
    # say) 23, "ఏంది" 16, "ఏంటమ్మా" 18, "అర్థం కాలేదు" (I didn't understand) 9.
    # These are not the caller taking the floor politely; they are the caller
    # telling us the last sentence did not land.
    "హలో", "హలొ", "hello", "helo", "ఏమన్నారు", "ఏమన్నారండి", "ఏంటి",
    "ఏంది", "ఏంటమ్మా", "ఏంటండి", "ఏమిటి", "ఏమండి", "ఎవరు", "ఎవరండి",
    # Refusal and do-not-call. These are compliance events, not conversation:
    # "నాకు call చేయొద్దు" was said 26 times and must land on the first
    # syllable, never after the bot finishes its paragraph.
    "వొద్దండి", "వద్దన్నాను", "చాలు", "చాలండి", "కట్",
})

INTERRUPT_PHRASES = (
    "ఒక నిమిషం", "ఒక్క నిమిషం", "ఒకనిమిషం", "ఒక్కనిమిషం",
    "ఒక సెకను", "ఒక్క సెకను", "ఒక్క క్షణం", "ఒక క్షణం",
    "one minute", "hold on", "one second",
    # --- added 10 Sep, each with its measured count in the run logs -------
    "ఇంట్రెస్ట్ లేదు",            # 24 + 16 with "నాకు" -- not interested
    "interest లేదు", "not interested",
    "call చేయొద్దు", "కాల్ చేయొద్దు", "ఫోన్ చేయొద్దు",   # 26 -- do not call
    "do not call", "dont call", "don t call",
    "మనిషితో మాట్లాడ",            # 13 -- I want a human
    "మనిషి తో మాట్లాడ", "మీరు robot", "మీరు రోబోట్",
    "అర్థం కాలేదు", "అర్ధం కాలేదు",   # 9 -- I did not understand
    "మాట్లాడనివ్వట్లేదు", "మాట్లాడనివ్వడం లేదు",  # 25 -- you aren't letting me speak
    "వినండి నేను", "నేను చెప్తే వినండి",
    "డబ్బు లేదు",                 # 15 -- no money; a hard refusal
)

# Strip punctuation ONLY.
#
# The obvious `[^\w\s]+` is wrong here and silently so. Python's `\w` does not
# match Telugu combining marks -- the matras and the anusvara -- so that
# pattern turns "ఆగండి" (wait) into "ఆగ డ", which matches nothing in either
# lexicon. Every Telugu word in this file would have been unreachable and the
# gate would have looked like it was simply ignoring the transcript. The
# explicit Indic range (Devanagari through Sinhala, which covers Telugu at
# U+0C00-U+0C7F) keeps them.
_PUNCT = re.compile(r"[^\w\sऀ-෿]+", re.UNICODE)


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(_PUNCT.sub(" ", (text or "").lower()).split())


def has_interrupt_word(text: str) -> bool:
    """True if the caller said something that must stop the bot now."""
    norm = normalise(text)
    if not norm:
        return False
    if any(phrase in norm for phrase in INTERRUPT_PHRASES):
        return True
    return any(token in INTERRUPT_WORDS for token in norm.split())


def is_backchannel(text: str, *, max_words: int = 2) -> bool:
    """True if the whole utterance is nothing but acknowledgement.

    Every token must be a backchannel AND the utterance must be short. "సరే
    అండి" is an acknowledgement; "సరే నేను ఇప్పుడు బిజీగా ఉన్నాను" starts with
    one and is not.
    """
    norm = normalise(text)
    if not norm:
        return False
    if has_interrupt_word(norm):
        return False
    tokens = norm.split()
    if not tokens or len(tokens) > max_words:
        return False
    return all(token in BACKCHANNELS for token in tokens)


@dataclass
class BargeInParams:
    """Every number the gate uses. All of them come from workflow config.

    Attributes:
        enabled: Master switch. False means this whole file is inert.
        min_speech_secs: Speech shorter than this, measured from the VAD onset,
            cannot take the floor from a speaking bot. A 150 ms blip is a cough,
            a line click or a backchannel -- never a sentence.
        min_rms_ratio: The caller's speech must be at least this many times the
            ambient floor measured on their own leg. 0 disables the check.
        max_backchannel_words: An utterance longer than this is never treated
            as a pure acknowledgement, whatever the words are.
        lexical: Whether to consult the transcript at all when one exists.
    """

    enabled: bool = False
    min_speech_secs: float = 0.0
    min_rms_ratio: float = 0.0
    max_backchannel_words: int = 2
    lexical: bool = True


@dataclass
class BargeInDecision:
    """The verdict plus WHY, because this will be read out of call logs."""

    interrupt: bool
    reason: str


class BargeInGate:
    """Decides whether the caller's current noise may stop the bot.

    Deliberately free of pipecat types so it can be tested, and reasoned about,
    as the small state machine it is. The strategy below feeds it frames.
    """

    # Class-level defaults so an instance built by a test with __new__, or one
    # asked for a decision before any audio arrives, still answers. This
    # codebase has shipped an AttributeError on exactly that shape before
    # (ReplyFilter._said, run 213).
    _bot_speaking = False
    _speech_started_at: float | None = None
    _text: str = ""
    _rms: float | None = None
    _floor: float | None = None

    # Smoothing constants for the two energy trackers. Not configurable: they
    # are the shape of an EMA, not a policy. The policy number is
    # `min_rms_ratio`, which IS configurable.
    _RMS_ALPHA = 0.4          # fast: what the caller is doing right now
    _FLOOR_ALPHA = 0.02       # slow: the line's ambient hiss

    def __init__(self, params: BargeInParams | None = None):
        self._params = params or BargeInParams()
        self._bot_speaking = False
        self._speech_started_at = None
        self._text = ""
        self._rms = None
        self._floor = None

    @property
    def params(self) -> BargeInParams:
        return self._params

    # --- observations ------------------------------------------------------

    def note_bot_speaking(self, speaking: bool) -> None:
        self._bot_speaking = speaking

    def note_speech_started(self, started_at: float | None = None) -> None:
        """Record the wall-clock instant the caller's speech actually began.

        VAD reports the onset LATE -- it has to hear `start_secs` of speech
        before it will commit -- and the frame carries both its decision
        timestamp and that delay, so the true onset is knowable. Using frame
        arrival time instead would under-count every utterance by `start_secs`
        (0.2 s on this agent) and make the duration floor mean something other
        than what it says.

        The first onset of a turn wins; later speaking frames within the same
        turn do not restart the clock.
        """
        if self._speech_started_at is None:
            self._speech_started_at = (
                started_at if started_at is not None else time.time()
            )

    def note_speech_stopped(self) -> None:
        self._speech_started_at = None
        self._text = ""

    def note_text(self, text: str) -> None:
        if text and text.strip():
            self._text = text

    def note_audio(self, audio: bytes, *, sample_rate: int = 8000) -> None:
        """Fold one inbound audio frame into the energy trackers.

        The ambient floor only learns while the caller is NOT speaking, so it
        stays a measure of the line and not of the caller.
        """
        rms = rms_int16(audio)
        if rms is None:
            return
        if self._rms is None:
            self._rms = rms
        else:
            self._rms = (1 - self._RMS_ALPHA) * self._rms + self._RMS_ALPHA * rms
        if self._speech_started_at is None:
            if self._floor is None:
                self._floor = rms
            else:
                self._floor = (
                    (1 - self._FLOOR_ALPHA) * self._floor + self._FLOOR_ALPHA * rms
                )

    def speech_secs(self, now: float | None = None) -> float:
        if self._speech_started_at is None:
            return 0.0
        now = now if now is not None else time.time()
        return max(0.0, now - self._speech_started_at)

    # --- the decision ------------------------------------------------------

    def should_interrupt(self, now: float | None = None) -> BargeInDecision:
        """Whether the turn starting right now may stop the bot.

        Never raises: any failure is reported as "interrupt", because a gate
        that errored closed would make the agent impossible to interrupt, and
        that is the worse of the two bugs by a wide margin.
        """
        try:
            return self._decide(now)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"[barge-in] gate failed open: {e!r}")
            return BargeInDecision(True, "gate-error")

    def _decide(self, now: float | None) -> BargeInDecision:
        p = self._params

        if not p.enabled:
            return BargeInDecision(True, "gate-disabled")

        # Not a barge-in at all: the bot is silent, so there is nothing to
        # interrupt and nothing to protect. Never gate the ordinary case.
        if not self._bot_speaking:
            return BargeInDecision(True, "bot-not-speaking")

        text = self._text

        # 1. Lexical, and FIRST, so an explicit stop word beats every other
        #    check. "ఆగండి" is three syllables and can land under the duration
        #    floor; it still has to stop the bot instantly.
        if p.lexical and text:
            if has_interrupt_word(text):
                return BargeInDecision(True, "interrupt-word")
            if is_backchannel(text, max_words=p.max_backchannel_words):
                return BargeInDecision(False, "backchannel")

        # 2. Duration. The usual case: no transcript has arrived yet, because
        #    STT is slower than the interruption decision. This is the check
        #    that actually does the work on a live call.
        if p.min_speech_secs > 0 and self._speech_started_at is not None:
            secs = self.speech_secs(now)
            if secs < p.min_speech_secs:
                return BargeInDecision(False, f"too-short:{secs:.3f}s")

        # 3. Energy, relative to the caller's own line. Only meaningful once
        #    both trackers have seen audio; skipped entirely otherwise, because
        #    a gate that guesses at silence is a gate that mutes real callers.
        if p.min_rms_ratio > 0 and self._rms is not None and self._floor:
            threshold = self._floor * p.min_rms_ratio
            if self._rms < threshold:
                return BargeInDecision(
                    False, f"quiet:{self._rms:.0f}<{threshold:.0f}"
                )

        return BargeInDecision(True, "allowed")


def rms_int16(audio: bytes) -> float | None:
    """RMS of signed 16-bit little-endian PCM, without numpy or audioop.

    `audioop` was removed in Python 3.13, which this runs on. Every fourth
    sample is enough for an energy estimate and keeps this at roughly 40
    operations per 20 ms frame.
    """
    if not audio or len(audio) < 2:
        return None
    samples = array("h")
    samples.frombytes(audio[: len(audio) - (len(audio) % 2)])
    if not samples:
        return None
    step = 4 if len(samples) >= 16 else 1
    total = 0
    count = 0
    for i in range(0, len(samples), step):
        s = samples[i]
        total += s * s
        count += 1
    if not count:
        return None
    return (total / count) ** 0.5


class BargeInGatedUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """Wraps a start strategy and gates only its interruption.

    The inner strategy decides WHEN the turn starts, exactly as before. This
    decides whether that turn start is also allowed to stop the bot.

    Wrapping rather than editing pipecat keeps the submodule clean and keeps
    the decision next to the rest of Vaani's turn-taking. It also means this
    composes with whichever strategy the workflow selected -- min_words,
    provisional_vad or the default pair -- without knowing anything about them.
    """

    def __init__(self, inner: BaseUserTurnStartStrategy, gate: BargeInGate, **kwargs):
        super().__init__(**kwargs)
        self._inner = inner
        self._gate = gate

        # The controller registers ITS handlers on this object, not on the
        # inner one, so the inner strategy's events would otherwise go nowhere.
        # Re-emit each of them as our own.
        inner.add_event_handler("on_push_frame", self._on_inner_push_frame)
        inner.add_event_handler("on_broadcast_frame", self._on_inner_broadcast_frame)
        inner.add_event_handler("on_user_turn_started", self._on_inner_user_turn_started)
        inner.add_event_handler("on_reset_aggregation", self._on_inner_reset_aggregation)

    @property
    def inner(self) -> BaseUserTurnStartStrategy:
        """The wrapped strategy.

        Named `inner` to match `DeferredUserTurnStopStrategy`, which is the
        name `turn_taking.analyzer_from` already walks.
        """
        return self._inner

    @property
    def gate(self) -> BargeInGate:
        return self._gate

    def __str__(self) -> str:
        return f"BargeInGated({self._inner})"

    async def setup(self, task_manager):
        await super().setup(task_manager)
        await self._inner.setup(task_manager)

    async def cleanup(self):
        await super().cleanup()
        await self._inner.cleanup()

    async def handle_user_turn_started(self):
        await self._inner.handle_user_turn_started()

    async def handle_user_turn_stopped(self):
        self._gate.note_speech_stopped()
        await self._inner.handle_user_turn_stopped()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        """Observe the frame, then hand it to the inner strategy untouched."""
        self.observe(frame)
        result = await self._inner.process_frame(frame)
        return ProcessFrameResult.CONTINUE if result is None else result

    def observe(self, frame: Frame) -> None:
        """Feed one frame into the gate. Synchronous and allocation-light."""
        if isinstance(frame, InputAudioRawFrame):
            self._gate.note_audio(frame.audio, sample_rate=frame.sample_rate)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            # `timestamp` is when VAD decided; `start_secs` is how much speech
            # it needed first. The true onset is the difference.
            timestamp = getattr(frame, "timestamp", None)
            onset = (
                timestamp - getattr(frame, "start_secs", 0.0)
                if timestamp is not None
                else None
            )
            self._gate.note_speech_started(onset)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._gate.note_speech_started()
        elif isinstance(frame, (VADUserStoppedSpeakingFrame, UserStoppedSpeakingFrame)):
            self._gate.note_speech_stopped()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._gate.note_bot_speaking(True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._gate.note_bot_speaking(False)
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            self._gate.note_text(frame.text)

    # --- inner strategy events --------------------------------------------

    async def _on_inner_push_frame(self, _strategy, frame, direction=None):
        if direction is None:
            await self.push_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _on_inner_broadcast_frame(self, _strategy, frame_cls, **kwargs):
        await self.broadcast_frame(frame_cls, **kwargs)

    async def _on_inner_reset_aggregation(self, _strategy):
        await self.trigger_reset_aggregation()

    async def _on_inner_user_turn_started(
        self, _strategy, params: UserTurnStartedParams
    ):
        """The one line this whole module exists for.

        The turn starts either way. Only `enable_interruptions` moves.
        """
        decision = self._gate.should_interrupt()
        enable = params.enable_interruptions and decision.interrupt

        if params.enable_interruptions and not enable:
            logger.info(
                f"[barge-in] interruption SUPPRESSED ({decision.reason}); "
                "the caller's turn still starts"
            )

        await self._call_event_handler(
            "on_user_turn_started",
            UserTurnStartedParams(
                enable_interruptions=enable,
                enable_user_speaking_frames=params.enable_user_speaking_frames,
            ),
        )


def resolve_barge_in_params(run_configs: dict) -> BargeInParams:
    """Read the gate's numbers out of workflow_configurations.

    Every one of them is a stored config key. This project has a standing rule
    against hardcoded milliseconds and it applies here in full: nothing about
    barge-in should require a deploy to change.
    """
    return BargeInParams(
        enabled=bool(run_configs.get(
            "barge_in_gate_enabled", DEFAULT_BARGE_IN_GATE_ENABLED)),
        min_speech_secs=max(0.0, float(run_configs.get(
            "barge_in_min_speech_secs", DEFAULT_BARGE_IN_MIN_SPEECH_SECS))),
        min_rms_ratio=max(0.0, float(run_configs.get(
            "barge_in_min_rms_ratio", DEFAULT_BARGE_IN_MIN_RMS_RATIO))),
        max_backchannel_words=max(1, int(run_configs.get(
            "barge_in_max_backchannel_words",
            DEFAULT_BARGE_IN_MAX_BACKCHANNEL_WORDS))),
        lexical=bool(run_configs.get("barge_in_lexical", DEFAULT_BARGE_IN_LEXICAL)),
    )


def apply_barge_in_gate(strategies, run_configs: dict):
    """Wrap start strategies in the gate, or return them untouched.

    Disabled -- the default -- returns the SAME list object with the SAME
    strategy instances. Not a copy, not a wrapper configured to allow
    everything: nothing is constructed at all, so there is no code path for it
    to change behaviour on.
    """
    params = resolve_barge_in_params(run_configs)
    if not params.enabled or not strategies:
        return strategies

    gate = BargeInGate(params)
    logger.info(
        f"[barge-in] confidence gate ENABLED min_speech={params.min_speech_secs}s "
        f"min_rms_ratio={params.min_rms_ratio} lexical={params.lexical}"
    )
    return [BargeInGatedUserTurnStartStrategy(s, gate) for s in strategies]
