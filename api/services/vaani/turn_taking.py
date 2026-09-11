"""Vaani owns turn-taking -- when the caller is judged to have finished.

Why this is Vaani's and not Dograh's
------------------------------------
Turn-taking is the single largest term in the latency budget and it is a
*conversation* decision, not plumbing. Measured on run 110 (2026-08-26):

    turn 3   total 1.684s   LLM TTFB 0.270s
    turn 4   total 1.628s   LLM TTFB 0.225s
    turn 5   total 1.365s   LLM TTFB 0.196s

The LLM is no longer the problem. Roughly 1.2s per turn is spent BEFORE the LLM
starts -- endpoint decision plus STT finalisation. That is the whole remaining
gap to 700 ms, and it is decided here.

The paired constraint, from `latency_budget.yaml` and non-negotiable
-------------------------------------------------------------------
Faster endpointing buys false interruptions, and a latency win purchased with
interruptions is NOT a win. Telugu callers on the incumbent already protested
being cut off at 350 ms.

    max_false_interruption_rate: 0.02
    min_endpoint_silence_ms_telugu: 600   # hard floor when NOT using a detector
    min_turn_words: 1                     # one-word Telugu turns MUST register

`smart_turn_stop_secs` currently defaults to 0.2, which is far below that
600 ms floor. That is defensible ONLY because a trained detector is deciding
the boundary rather than a silence timer -- and only for as long as
smart-turn-v3 is actually reading Telugu prosody correctly. Nobody has measured
that. If false interruptions appear on real Telugu calls, raise this first.
"""

from __future__ import annotations

from loguru import logger

from api.services.vaani.telugu_turn import (
    TeluguTurnAnalyzer,
    TeluguTurnParams,
)
from api.schemas.workflow_configurations import (
    DEFAULT_BLIND_MIN_SILENCE_MS,
    DEFAULT_BLIND_SHORT_SILENCE_MS,
    DEFAULT_ENDPOINT_FRAGMENT_FLOOR_SECS,
    DEFAULT_ENDPOINT_UNSURE_BAND,
    DEFAULT_ENDPOINT_UNSURE_FLOOR_SECS,
    DEFAULT_TURN_CUTOFFS_BEFORE_ADAPTING,
    DEFAULT_TURN_MODEL,
    DEFAULT_TURN_FRAGMENT_SECS,
    DEFAULT_TURN_MIN_SILENCE_MS,
    DEFAULT_TURN_RESUME_WINDOW_SECS,
    DEFAULT_TURN_SHORT_THRESHOLD,
    DEFAULT_TURN_WINDOW_SECS,
    DEFAULT_ENDPOINT_MAX_SECS,
    DEFAULT_ENDPOINT_MIN_SECS,
    DEFAULT_PROVISIONAL_VAD_PAUSE_SECS,
    DEFAULT_SEMANTIC_TURN_COMPLETION,
    DEFAULT_SMART_TURN_STOP_SECS,
    DEFAULT_TURN_START_MIN_WORDS,
    DEFAULT_TURN_START_STRATEGY,
    DEFAULT_TURN_STOP_STRATEGY,
    DEFAULT_DOGRAH_SPEECH_TIMEOUT_SECS,
    DEFAULT_TELUGU_TURN_ASSIST,
    DEFAULT_TURN_WAIT_FOR_TRANSCRIPT,
)
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.turns.user_stop.deferred_user_turn_stop_strategy import deferred
from pipecat.turns.user_stop.llm_turn_completion_user_turn_stop_strategy import (
    LLMTurnCompletionUserTurnStopStrategy,
)
from pipecat.turns.user_turn_completion_mixin import UserTurnCompletionConfig
from api.services.vaani.compiler import MODE_PROTOCOL
from api.services.vaani.filler_turns import apply_filler_guard
from api.services.vaani.turn_completion import compose_instructions
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_start import (
    ExternalUserTurnStartStrategy,
    MinWordsUserTurnStartStrategy,
    ProvisionalVADUserTurnStartStrategy,
)
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import (
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import (
    ExternalUserTurnStopStrategy,
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)


class TextAwareTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """The stock strategy, plus: the analyzer is told what was said.

    Pipecat keeps the transcript to itself -- `_handle_transcription` stores it
    on the strategy and the analyzer only ever sees audio. That is the right
    default for a prosody model, and wrong here, because half of what makes a
    Telugu answer unfinished is not audible. "అరవై" (sixty) and "సరే" (fine) are
    the same shape of sound; only the words say that one of them is a quantity
    with the unit still to come.

    Overriding rather than editing pipecat keeps the submodule clean and keeps
    this decision where the rest of Vaani's turn-taking lives.
    """

    async def _handle_transcription(self, frame) -> None:
        note = getattr(self._turn_analyzer, "note_text", None)
        if note is not None:
            note(frame.text)
        await super()._handle_transcription(frame)


def resolve_turn_start_min_words(run_configs: dict) -> int:
    return max(
        1,
        int(run_configs.get("turn_start_min_words", DEFAULT_TURN_START_MIN_WORDS)),
    )


def resolve_provisional_vad_pause_secs(run_configs: dict) -> float:
    return max(
        0.1,
        float(
            run_configs.get(
                "provisional_vad_pause_secs", DEFAULT_PROVISIONAL_VAD_PAUSE_SECS
            )
        ),
    )


def create_user_turn_start_strategies(
    run_configs: dict, *, uses_external_turns: bool
):
    """When does the caller's turn BEGIN."""

    turn_start_strategy = run_configs.get(
        "turn_start_strategy", DEFAULT_TURN_START_STRATEGY
    )

    if turn_start_strategy == "min_words":
        return [
            MinWordsUserTurnStartStrategy(
                min_words=resolve_turn_start_min_words(run_configs)
            )
        ]

    if turn_start_strategy == "provisional_vad":
        return [
            ProvisionalVADUserTurnStartStrategy(
                pause_secs=resolve_provisional_vad_pause_secs(run_configs)
            ),
        ]

    if uses_external_turns:
        # The STT emits its own turn boundaries and owns interruptions. Local
        # VAD is deliberately kept out of the default start strategies: it would
        # win the race on raw voice activity and start the turn before the STT
        # confirms a real turn.
        return [ExternalUserTurnStartStrategy(enable_interruptions=True)]

    return [TranscriptionUserTurnStartStrategy(), VADUserTurnStartStrategy()]


def create_user_turn_stop_strategies(
    run_configs: dict, *, uses_external_turns: bool
):
    """When is the caller judged to have FINISHED. The expensive decision.

    The filler guard is applied HERE, around every branch below, rather than
    inside one of them. That placement is the whole point of it: the knowledge
    that "ఆ" is a thinking noise lived only in the `turn_analyzer` branch, so
    switching to `transcription` -- pipecat's fixed-timeout strategy, which is
    what runs after the detector comparison on 10 Sep -- silently dropped it,
    and five of run 863's twenty turns became replies to a single syllable.
    Wrapping the result covers whichever detector is configured, now and later.

    External turns are deliberately NOT wrapped: another system owns turn
    boundaries there and second-guessing it from inside would be a bug.
    """

    if uses_external_turns:
        return [ExternalUserTurnStopStrategy()]

    return apply_filler_guard(
        _build_user_turn_stop_strategies(run_configs), run_configs)


def _telugu_analyzer(run_configs: dict, *, stop_secs: float | None = None):
    """Build the Telugu turn analyzer from config. One definition, two callers.

    Extracted when `telugu_turn_assist` was added, so the assist path and the
    `turn_analyzer` path cannot drift apart. The fourteen values below were
    hard-won -- until 7 Sep only four of them were reachable from config and the
    rest needed a deploy, including the 450 ms short-blind floor the client
    objected to by name. Two copies of that list would be two things to keep in
    step, and the failure mode of getting it wrong is a detector that behaves
    differently depending on which switch turned it on.

    Returns None if the model cannot be built at all; callers decide what to do
    about it, because the right answer differs -- the `turn_analyzer` path has
    to fall back to something, the assist path simply declines to assist.
    """
    if stop_secs is None:
        stop_secs = run_configs.get(
            "smart_turn_stop_secs", DEFAULT_SMART_TURN_STOP_SECS)

    kind = str(run_configs.get("turn_model", DEFAULT_TURN_MODEL))
    if kind == "audio-native":
        from api.services.vaani.audio_native_turn import AudioNativeTurnAnalyzer
        analyzer_cls = AudioNativeTurnAnalyzer
    else:
        analyzer_cls = TeluguTurnAnalyzer

    try:
        return analyzer_cls(params=TeluguTurnParams(
            stop_secs=stop_secs,
            min_endpoint_secs=float(run_configs.get(
                "endpoint_min_secs", DEFAULT_ENDPOINT_MIN_SECS)),
            max_endpoint_secs=float(run_configs.get(
                "endpoint_max_secs", DEFAULT_ENDPOINT_MAX_SECS)),
            fragment_floor_secs=float(run_configs.get(
                "endpoint_fragment_floor_secs",
                DEFAULT_ENDPOINT_FRAGMENT_FLOOR_SECS)),
            unsure_floor_secs=float(run_configs.get(
                "endpoint_unsure_floor_secs",
                DEFAULT_ENDPOINT_UNSURE_FLOOR_SECS)),
            unsure_band=float(run_configs.get(
                "endpoint_unsure_band", DEFAULT_ENDPOINT_UNSURE_BAND)),
            blind_min_silence_ms=float(run_configs.get(
                "blind_min_silence_ms", DEFAULT_BLIND_MIN_SILENCE_MS)),
            blind_short_silence_ms=float(run_configs.get(
                "blind_short_silence_ms", DEFAULT_BLIND_SHORT_SILENCE_MS)),
            fragment_secs=float(run_configs.get(
                "turn_fragment_secs", DEFAULT_TURN_FRAGMENT_SECS)),
            short_threshold=float(run_configs.get(
                "turn_short_threshold", DEFAULT_TURN_SHORT_THRESHOLD)),
            min_silence_ms=float(run_configs.get(
                "turn_min_silence_ms", DEFAULT_TURN_MIN_SILENCE_MS)),
            window_secs=float(run_configs.get(
                "turn_window_secs", DEFAULT_TURN_WINDOW_SECS)),
            resume_window_secs=float(run_configs.get(
                "turn_resume_window_secs", DEFAULT_TURN_RESUME_WINDOW_SECS)),
            cutoffs_before_adapting=int(run_configs.get(
                "turn_cutoffs_before_adapting",
                DEFAULT_TURN_CUTOFFS_BEFORE_ADAPTING)),
        ),
            model_kind=kind,
        )
    except Exception as exc:
        # A missing weights file or a bad ONNX runtime must never take a call
        # down. Declining to assist is a degraded call; raising here is no call.
        logger.warning(f"[turn] Telugu analyzer could not be built: {exc}")
        return None


def _build_user_turn_stop_strategies(run_configs: dict):
    """The detector itself, unwrapped. See the caller for why the split."""

    # The default MUST be supplied here. A bare .get() returned None for any
    # agent whose workflow_configurations is {} -- which is every agent created
    # through the new editor -- so the declared `turn_analyzer` default was
    # never reached and every call fell through to the fixed 0.6s speech
    # timeout below. Measured cost: a dead-constant 0.80s endpoint on runs 110
    # and 163. See test_turn_stop_default_is_applied.py.
    strategy = run_configs.get("turn_stop_strategy", DEFAULT_TURN_STOP_STRATEGY)

    if strategy == "turn_analyzer":
        stop_secs = run_configs.get(
            "smart_turn_stop_secs", DEFAULT_SMART_TURN_STOP_SECS
        )
        # Smart Turn v3 covers 23 languages and Telugu is not one of them, so on
        # a Telugu call it rarely returns COMPLETE and the turn ends on the
        # silence timeout instead of on a decision. Measured on run 213 that
        # timeout was 0.693s of a 1.05s turn -- the largest single cost, larger
        # than the LLM at 0.245s.
        #
        # TeluguTurnAnalyzer is trained on this agent's own caller recordings
        # and ends 33.9% of turns early at under a 2% false-cutoff rate. On the
        # turns it is not confident about it returns INCOMPLETE, which leaves
        # today's behaviour exactly as it is: it can make a turn faster, never
        # slower. If its weights are missing it reports `enabled = False` and we
        # fall back rather than run a detector that can never fire.
        # Two-sided endpointing. `stop_secs` stays the fallback for when the
        # model cannot load; these three are what the model spends its verdict
        # on when it can. See DEFAULT_ENDPOINT_* for why a single number was
        # never going to work.
        # `audio-native` reads the turn off the waveform through a Whisper-tiny
        # encoder fine-tuned on this agent's own Telugu calls, instead of the 16
        # hand-built prosody numbers. It is a SUBCLASS of TeluguTurnAnalyzer --
        # every endpoint timer below is inherited unchanged, only the
        # probability is replaced -- so the constructor signature is identical
        # and a missing model file degrades to prosody rather than failing.
        #
        # Measured on 30 real recordings (tools/sweep_audio_native.py):
        #   prosody forest (was live)   23.0% cut off @ 0.48s wait
        #   audio-native                16.0% cut off @ 0.76s wait
        # and, the check that matters, a model-free stopwatch made to wait the
        # same 0.76s scores 20.8%. It beats the control, which the retrained
        # linear model did not (docs 30).
        analyzer = _telugu_analyzer(run_configs, stop_secs=stop_secs)
        if analyzer is None or not analyzer.enabled:
            logger.warning(
                "[turn] Telugu analyzer unavailable; falling back to Smart Turn "
                "v3, which has no Telugu and will mostly time out"
            )
            analyzer = LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=stop_secs))
        detector = TextAwareTurnStopStrategy(
                turn_analyzer=analyzer,
                # Let the semantic turn detector end the turn on its own verdict
                # instead of waiting for the final transcript (measured 438 ms
                # p50 after flush). The transcript becomes bookkeeping and comes
                # off the latency critical path. `user_turn_stop_timeout` is
                # still the safety net, and SpeculationProbe measures how often
                # the text we already hold equals the final text.
                wait_for_transcript=run_configs.get(
                    "turn_wait_for_transcript", DEFAULT_TURN_WAIT_FOR_TRANSCRIPT
                ),
        )

        if not run_configs.get("semantic_turn_completion",
                               DEFAULT_SEMANTIC_TURN_COMPLETION):
            return [detector]

        # SEMANTIC turn completion: the LLM decides, the timer does not.
        #
        # Run 790, the call the client complained about:
        #
        #     USER : ...ఇండస్ట్రియల్ ఏరియాలో ఉంటాను.
        #     USER : సిటీకి కొంచెం బయట.          <- still talking
        #     BOT  : సరే, మీకు సొంత              <- cut in
        #
        # "ఇండస్ట్రియల్ ఏరియాలో ఉంటాను" is a grammatically COMPLETE sentence.
        # The prosody model hears a falling contour, and `completeness` finds no
        # dangling quantity, no open range, no connective and no hesitation. Both
        # signals say finished and both are wrong. Grammatically complete is not
        # conversationally complete, and neither a timer nor a grammar rule can
        # separate them -- only something that follows the conversation can.
        #
        # So the analyzer is DEFERRED: it still decides when to ASK the question,
        # which keeps the only detector that reads Telugu prosody in the loop and
        # keeps `analyzer_from` able to hand the same instance to the filler
        # player. The LLM ANSWERS it, with the marker protocol -- and on ○ or ◐
        # the turn is not finalized, so the caller keeps the floor.
        #
        # This needs no interim transcripts. An earlier note in this project
        # said it was blocked on realtime STT; that is true only of the LATENCY
        # benefit -- thinking ahead mid-utterance -- and not of this, which is
        # the correctness benefit.
        #
        # Vaani's own instructions, not pipecat's: pipecat's demand that every
        # reply BEGIN with the marker, and MODE_PROTOCOL demands it begin with
        # MODE. `compose_instructions` orders them -- marker, MODE, speech --
        # so both survive. MODE: END is the only thing that hangs up a call.
        logger.info("Semantic turn completion ENABLED (LLM gates the turn end)")
        return [
            deferred(detector),
            LLMTurnCompletionUserTurnStopStrategy(
                config=UserTurnCompletionConfig(
                    instructions=compose_instructions(MODE_PROTOCOL),
                ),
            ),
        ]

    # Dograh's own strategy -- pipecat's fixed timer -- which is what these
    # agents actually run, deliberately: the custom Telugu detector was tried
    # and judged worse, and this was chosen over it on 10 Sep.
    #
    # It was also built with NO arguments, so `user_speech_timeout` took
    # pipecat's 0.6s default and nothing could change it without editing this
    # line. With Silero's `stop_secs=0.2` on top that is a hard 0.8s before the
    # LLM is asked anything, on every turn -- which is exactly the floor
    # measured on 11 Sep: 0 of 69 turns came in under 0.6s, minimum 0.831s,
    # across all four agents.
    #
    # Worth being explicit, because it has misled this project already: the
    # `endpoint_*` and `turn_model` values stored on wf2 are read ONLY inside
    # the `turn_analyzer` branch above. On this path they do nothing. Someone
    # set MB Solar's `endpoint_min_secs` to 0.3 and it changed nothing.
    timer = SpeechTimeoutUserTurnStopStrategy(
        user_speech_timeout=float(run_configs.get(
            "dograh_speech_timeout_secs", DEFAULT_DOGRAH_SPEECH_TIMEOUT_SECS)),
    )

    if not run_configs.get("telugu_turn_assist",
                           DEFAULT_TELUGU_TURN_ASSIST):
        return [timer]

    # ASSIST, not replace. Both strategies are live and the controller ends the
    # turn on whichever fires FIRST (`_trigger_user_turn_start` guards against a
    # double start; `_on_user_turn_stopped` is called by any strategy). So the
    # timer remains the backstop it is today and the Telugu model can only end a
    # turn EARLIER, never later.
    #
    # Which is the whole trade, and it is one-sided, so it is written down
    # rather than discovered on a live call: the model's only power here is to
    # jump in before 0.6s. When it is right the agent is faster; when it is
    # wrong it talks over the caller. Adding it CANNOT reduce cut-offs -- two
    # detectors do not check each other, the eager one simply wins. If the
    # reason this model was rejected was that it cut people off, this
    # arrangement inherits that and does not cure it.
    #
    # Off by default: today's behaviour is the default, and turning this on is
    # a deliberate act with a measurement attached to it.
    analyzer = _telugu_analyzer(run_configs)
    if analyzer is None or not getattr(analyzer, "enabled", False):
        logger.warning("[turn] telugu_turn_assist requested but the model "
                       "could not be loaded; running Dograh's timer alone")
        return [timer]
    logger.info("[turn] Telugu assist ENABLED alongside Dograh's timer -- "
                "whichever decides first ends the turn")
    return [timer, TextAwareTurnStopStrategy(
        turn_analyzer=analyzer,
        wait_for_transcript=run_configs.get(
            "turn_wait_for_transcript", DEFAULT_TURN_WAIT_FOR_TRANSCRIPT),
    )]


def analyzer_from(strategies) -> object | None:
    """The turn analyzer inside a stop strategy, if there is one.

    The filler player needs the SAME analyzer instance the turn strategy is
    using -- it reads the probability that model just computed rather than
    running its own. Building a second one would score different audio and the
    two would disagree about whether the caller had finished.
    """
    for strategy in strategies or []:
        # Unwrap first. `DeferredUserTurnStopStrategy` holds the real strategy
        # in `.inner` and defines neither `_turn_analyzer` nor `__getattr__`, so
        # the lookup below fell straight through it and returned None -- and a
        # None analyzer means `FillerPlayer` is constructed inert and never
        # speaks. Enabling semantic turn completion therefore switched the
        # fillers off, silently, at exactly the moment the gaps got longer. It
        # took a live call to notice, and nothing failed or logged.
        #
        # Walked as a chain rather than one unwrap, because a wrapper can wrap
        # a wrapper and the failure mode of getting this wrong is silence.
        seen = 0
        while strategy is not None and seen < 8:
            analyzer = getattr(strategy, "_turn_analyzer", None) or getattr(
                strategy, "turn_analyzer", None)
            if analyzer is not None:
                return analyzer
            strategy = getattr(strategy, "inner", None)
            seen += 1
    return None
