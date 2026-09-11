"""A thinking noise is not a turn. Wait for the sentence behind it.

The defect, measured on run 863
-------------------------------
The caller was asked for his electricity bill and said, over five seconds:

    17:21:16  "ఆ"                 -> a full LLM reply was generated
    17:21:19  "ఉ"                 -> a second full LLM reply was generated
    17:21:20  BOT  which property type?
    17:21:21  BOT  what is your monthly bill?

Two single syllables -- pure hesitation, the sound of a man reaching for a
number -- each became a finished user turn, each triggered a generation, and
both replies were spoken 1.3 s apart. **Five of that call's twenty user turns
were bare fillers.** It is the largest single source of the agent answering
things nobody said, and of it appearing to talk over itself.

Why the existing guard does not cover this
-------------------------------------------
`completeness.sounds_unfinished()` already knows "ఆ" is not a sentence, and
`TeluguTurnAnalyzer` consults it. But that lives inside the `turn_analyzer`
branch of `create_user_turn_stop_strategies`. On `turn_stop_strategy =
"transcription"` -- pipecat's `SpeechTimeoutUserTurnStopStrategy`, which is
what the client asked to run after comparing the two detectors -- the analyzer
is never constructed, so the filler knowledge is simply absent. The strategy
waits its timeout, sees *a* transcript, and finalises the turn.

So the knowledge has to sit somewhere both paths reach. This wraps whichever
stop strategy is configured, exactly as `BargeInGatedUserTurnStartStrategy`
wraps whichever start strategy is configured, and for the same reason: the
decision belongs next to the rest of Vaani's turn-taking, and pipecat stays
unedited.

Deferring, not discarding -- and why that distinction is load-bearing
----------------------------------------------------------------------
Run 314's lesson stands: a bare "um" IS a real answer to "is anyone there?",
and an empty name is worse than a wrong one. So a filler turn is never thrown
away. It is held for `defer_secs` to see whether the sentence it was
introducing arrives.

If it does, the caller gets one reply to what he actually meant.
If it does not, the turn is released unchanged and the agent answers the
filler -- today's behaviour, just later.

The deferral is bounded twice over, because a guard that can strand a caller in
silence is worse than the bug it fixes:

  * `defer_secs` -- a watchdog fires the turn end even if nothing else is ever
    heard. Nothing depends on the caller speaking again.
  * `max_defers` -- a caller who says "ఆ ... ఆ ... ఆ" cannot be deferred
    indefinitely; after this many holds in one turn the next one goes straight
    through.

Off by default. `filler_turn_guard_enabled` is False and `apply_filler_guard`
then returns the caller's list unchanged -- the same objects, not copies.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from api.schemas.workflow_configurations import (
    DEFAULT_FILLER_TURN_DEFER_SECS,
    DEFAULT_FILLER_TURN_GUARD_ENABLED,
    DEFAULT_FILLER_TURN_MAX_DEFERS,
)
from api.services.vaani import completeness
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
    BaseUserTurnStopStrategy,
    UserTurnStoppedParams,
)


def is_only_filler(text: str) -> bool:
    """True when the whole utterance is hesitation and nothing else.

    Kept for logging and for tests that want the narrow case by name. The
    HOLD decision uses `should_hold` below, which is broader.

    `strip_fillers` is deliberately not used. It returns the original string
    when stripping would empty it (run 314), which is right for storing a value
    and exactly wrong for asking "was that anything at all".
    """
    said = (text or "").strip()
    if not said:
        return False
    tokens = [t.strip(completeness._PUNCT).lower() for t in said.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    # Two syllables of hesitation is still hesitation ("ఆ ఆ", "um uh"); a third
    # word means he is saying something, whatever it sounds like.
    if len(tokens) > 2:
        return False
    return all(t in completeness.HESITATIONS for t in tokens)


def should_hold(text: str) -> bool:
    """True when the sentence cannot end where it stands, so wait for the rest.

    This is `completeness.sounds_unfinished` and nothing else, on purpose. A
    second copy of "what is an unfinished Telugu sentence" is a second thing to
    drift, and that drift is precisely the failure this module exists to fix:
    the knowledge was already in the codebase, correct and tested, and merely
    out of reach on the `transcription` code path.

    Checked against every caller turn in run 863. It holds all four that should
    have been held -- "ఆ", "ఉ", "ఒక", and the dangling quantity "అరవై" whose
    unit had not been said yet (the run-295 lineage that once stored a monthly
    bill of 15) -- and releases every real answer, including the ones a cruder
    rule gets wrong:

        "ఆ ఉంది"            -> release. "ఆ" leads, but he answered.
        "ఉన్నాము ఉన్నాము"    -> release. Repetition is emphasis, not hesitation.
        "చెప్పండి"          -> release. An instruction, not a noise.
        "సొంతమే"            -> release. The answer to a question.

    An empty transcript is NOT held. No text at all is the STT having nothing
    to say, which is a different condition from the caller trailing off, and
    holding on it would delay every turn the transcript is merely late for.
    """
    if not (text or "").strip():
        return False
    return completeness.sounds_unfinished(text)


class FillerAwareUserTurnStopStrategy(BaseUserTurnStopStrategy):
    """Wraps a stop strategy and holds back turns that are only a filler.

    Forwards every frame to the inner strategy untouched, so the inner
    strategy's own detection is unchanged. Only the moment of
    `on_user_turn_stopped` moves, and only when the transcript for the turn is
    nothing but hesitation.
    """

    def __init__(self, inner: BaseUserTurnStopStrategy, *,
                 defer_secs: float = 1.2, max_defers: int = 2,
                 backstop_secs: float | None = None,
                 in_flight: 'ReplyInFlight | None' = None, **kwargs):
        super().__init__(**kwargs)
        self._inner = inner
        self._defer_secs = max(0.0, float(defer_secs))
        self._max_defers = max(0, int(max_defers))

        self._text = ""
        self._defers = 0
        self._timer: asyncio.Task | None = None
        # The params of the turn currently being held, so a withdrawal can
        # leave a watchdog behind instead of nothing. See `_rearm_backstop`.
        self._held_params = None
        self._in_flight = in_flight
        # Long enough that the inner strategy virtually always wins the race,
        # short enough that a stranded turn is never the 5.0s backstop plus a
        # hold. Runs 885 and 887 measured 8.055s and 6.406s of dead air.
        self._backstop_secs = (max(self._defer_secs, 1.5)
                               if backstop_secs is None
                               else max(0.0, float(backstop_secs)))

        inner.add_event_handler("on_push_frame", self._on_inner_push_frame)
        inner.add_event_handler("on_user_turn_stopped", self._on_inner_stopped)
        # Relay the rest, but only the ones this particular strategy declares.
        # `BaseObject.add_event_handler` LOGS a warning and carries on for an
        # unregistered event rather than raising, so a blanket try/except
        # catches nothing and every call would start with warning noise that
        # looks like a fault. Which events exist differs by strategy --
        # SpeechTimeout has no `on_reset_aggregation`, TurnAnalyzer does.
        declared = set(getattr(inner, "_event_handlers", {}) or {})
        for event in ("on_user_turn_inference_triggered",
                      "on_broadcast_frame", "on_reset_aggregation"):
            if event in declared:
                inner.add_event_handler(event, self._relay(event))

    # --- plumbing ---------------------------------------------------------

    @property
    def inner(self) -> BaseUserTurnStopStrategy:
        """Named to match `DeferredUserTurnStopStrategy`, which
        `turn_taking.analyzer_from` already walks looking for an analyzer."""
        return self._inner

    def __str__(self) -> str:
        return f"FillerAware({self._inner})"

    def _relay(self, event: str):
        async def handler(_strategy, *args, **kwargs):
            await self._call_event_handler(event, *args, **kwargs)
        return handler

    async def _on_inner_push_frame(self, _strategy, frame, direction=None):
        if direction is None:
            await self.push_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def setup(self, task_manager):
        await super().setup(task_manager)
        await self._inner.setup(task_manager)

    async def cleanup(self):
        self._cancel_timer()
        await super().cleanup()
        await self._inner.cleanup()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        self._observe(frame)
        result = await self._inner.process_frame(frame)
        return ProcessFrameResult.CONTINUE if result is None else result

    def _observe(self, frame: Frame) -> None:
        # Two jobs, and the second one cost run 888.
        #
        # ACCUMULATE, because Sarvam splits one utterance across finals -- that
        # is the whole reason this class exists.
        #
        # WITHDRAW the hold when he speaks again, so the inner strategy decides
        # the turn afresh on the fuller sentence. That has to key on the VAD
        # edge: it is the only signal that arrives BEFORE the words, and
        # withdrawing on the text instead releases the turn the moment a
        # fragment lands. Run 888 is what that sounds like -- one sentence torn
        # into two turns, each answered separately, two bot replies on nearly
        # every exchange: "ఎన్ని సార్లు అడుగుతారండి దీన్ని?"
        #
        # But withdrawing is not the same as abandoning, and that was the
        # ORIGINAL bug. `_cancel_timer` alone handed the turn back to pipecat's
        # speech timeout, built with `wait_for_transcript=True`. Noise carries
        # no transcript, so the inner strategy never fired again and the turn
        # sat stranded until the 5.0s backstop:
        #
        #     1.2 + 5.0             = 6.2s   vs run 887's 6.406s
        #     1.2 + 1.2 + 5.0 + 0.8 = 8.2s   vs run 885's 8.055s
        #
        # So the withdrawal now re-arms a LAST-RESORT timer instead of leaving
        # nothing behind. The inner strategy is still free to end the turn
        # first, and normally does; this only guarantees that a turn which was
        # once held can never be stranded by a noise.
        if isinstance(frame, TranscriptionFrame):
            self._text = f"{self._text} {frame.text}".strip()
        elif isinstance(frame, InterimTranscriptionFrame):
            if frame.text and frame.text.strip():
                self._text = f"{self._text} {frame.text}".strip()
        elif isinstance(frame, (UserStartedSpeakingFrame,
                                VADUserStartedSpeakingFrame)):
            if self._timer is not None and not self._timer.done():
                self._rearm_backstop()

    # --- the decision -----------------------------------------------------

    async def _on_inner_stopped(self, _strategy, params: UserTurnStoppedParams):
        text = self._text

        # A reply to his LAST breath is already being built and he has not
        # heard any of it. Answering this one too means answering twice -- run
        # 889, four times in a sixty-second call. Hold it: when the turn is
        # released the accumulated text goes out as ONE turn, and he gets one
        # reply covering everything he said.
        busy = self._in_flight is not None and self._in_flight.unheard
        if busy and self._defer_secs > 0 and self._defers < self._max_defers:
            self._defers += 1
            self._held_params = params
            logger.info(
                f"[filler] a reply is already being built; holding {text!r} "
                f"for {self._defer_secs:.2f}s so he is answered once "
                f"(hold {self._defers}/{self._max_defers})")
            self._cancel_timer()
            self._timer = asyncio.create_task(self._release_later(params))
            return

        if (self._defer_secs <= 0
                or self._defers >= self._max_defers
                or not should_hold(text)):
            await self._release(params)
            return

        self._defers += 1
        self._held_params = params
        why = "a filler" if is_only_filler(text) else "unfinished"
        logger.info(
            f"[filler] holding {why} turn {text!r} for {self._defer_secs:.2f}s "
            f"(hold {self._defers}/{self._max_defers}); waiting for the rest")
        self._cancel_timer()
        self._timer = asyncio.create_task(self._release_later(params))

    def _rearm_backstop(self) -> None:
        """Withdraw the hold, but leave a watchdog behind.

        A turn that was held once must never be able to hang. The inner
        strategy almost always ends it long before this fires; when a noise
        stops the inner strategy from ever firing again, this is what stops the
        caller hearing silence.
        """
        params = self._held_params
        self._cancel_timer()
        if params is not None:
            self._timer = asyncio.create_task(
                self._release_later(params, secs=self._backstop_secs))

    async def _release_later(self, params: UserTurnStoppedParams,
                             secs: float | None = None) -> None:
        """The watchdog. Nothing here depends on the caller speaking again.

        Without this a caller whose entire answer is "ఆ" -- which run 314 shows
        is sometimes a real answer -- would be met with silence until the idle
        timeout. Deferring must never become discarding.
        """
        try:
            await asyncio.sleep(self._defer_secs if secs is None else secs)
        except asyncio.CancelledError:
            return
        logger.info("[filler] nothing followed; releasing the filler turn")
        await self._release(params)

    async def _release(self, params: UserTurnStoppedParams) -> None:
        # From here a reply is owed. Any turn that ends before it is spoken is
        # part of the same breath and must not earn a second answer.
        if self._in_flight is not None:
            self._in_flight.owe()
        self._cancel_timer()
        self._text = ""
        self._defers = 0
        self._held_params = None
        await self._call_event_handler("on_user_turn_stopped", params)

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def handle_user_turn_started(self):
        self._cancel_timer()
        self._text = ""
        self._defers = 0
        self._held_params = None
        handler = getattr(self._inner, "handle_user_turn_started", None)
        if handler is not None:
            await handler()


class ReplyInFlight:
    """Is a reply being built right now, with none of it spoken yet?

    Shared between `ReplyFilter`, which knows, and the turn guard, which needs
    to know. One instance per call.

    Run 889. The caller said one thing in two breaths and was answered twice:

        18:09:52.966  USER  ఆ సరే. మాట్లాడవచ్చు.
        18:09:54.730  USER  సార్ అండి.                 second final, 1.8s later
        18:09:56.984  BOT   [reply to the first]
        18:09:58.544  BOT   [reply to the second]

    Both finals landed before any audio was produced, each became a turn, and
    each got its own reply. He told us four times what that sounds like from
    his end -- "ఒకే క్వశ్చన్, రెండు క్వశ్చన్లు" (one question, two questions),
    "ఒక్క క్వశ్చన్ ఒకసారి అడగండి" (ask one question at a time) -- and hung up.

    `_stale` in the reply path cannot catch this: neither reply is stale, both
    were generated after their own turn ended and neither was overtaken.

    The window is deliberately narrow -- generation started, nothing spoken.
    Once audio is out, this must be FALSE: interrupting a caller who talks over
    the agent is barge-in, and holding his turn then would break it.
    """

    def __init__(self):
        self.generating = False
        self.spoken = False

    def owe(self) -> None:
        """A turn has ended, so a reply is owed -- the LLM has not started yet.

        This is the window run 892 fell through. `begin()` is called when the
        generation starts, and the second caller final can arrive BEFORE that:

            36.365  USER  [second final]
            37.545  BOT   [reply to the FIRST final]

        The reply to the first was still being decided at 36.365, but nothing
        had asked for it yet, so `unheard` was False and the turn was not
        merged. He got two answers and said so four times, ending with
        "డోంట్ ఫ్రస్ట్రేట్ మేడం".

        A reply is owed from the moment the turn ends, not from the moment the
        model is called.
        """
        self.generating = True
        self.spoken = False

    def begin(self) -> None:
        self.generating = True
        self.spoken = False

    def note_spoken(self) -> None:
        self.spoken = True

    def done(self) -> None:
        self.generating = False
        self.spoken = False

    @property
    def unheard(self) -> bool:
        """A reply exists, and the caller has not heard a word of it yet."""
        return bool(self.generating and not self.spoken)


def apply_filler_guard(strategies, run_configs: dict, in_flight=None):
    """Wrap the stop strategies in the filler guard, or return them untouched.

    Disabled -- the default -- returns the SAME list object with the SAME
    strategy instances. Nothing is constructed, so there is no code path by
    which it can change behaviour.
    """
    if not run_configs.get("filler_turn_guard_enabled",
                           DEFAULT_FILLER_TURN_GUARD_ENABLED):
        return strategies
    if not strategies:
        return strategies

    defer = float(run_configs.get("filler_turn_defer_secs",
                                  DEFAULT_FILLER_TURN_DEFER_SECS))
    max_defers = int(run_configs.get("filler_turn_max_defers",
                                     DEFAULT_FILLER_TURN_MAX_DEFERS))
    logger.info(f"[filler] turn guard ENABLED defer={defer}s "
                f"max_defers={max_defers}")
    # Only the LAST strategy emits `on_user_turn_stopped` in the two-strategy
    # semantic arrangement; wrapping that one is what matters, and wrapping a
    # single-element list is the ordinary case.
    return strategies[:-1] + [
        FillerAwareUserTurnStopStrategy(strategies[-1], defer_secs=defer,
                                        max_defers=max_defers,
                                        in_flight=in_flight)
    ]
