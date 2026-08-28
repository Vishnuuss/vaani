"""Vaani owns the voice pipeline DEFINITION.

What this does and does not claim
---------------------------------
Dograh has no voice pipeline of its own — it runs **Pipecat**, an independent
framework from Daily. So "Vaani replaces Dograh's pipeline" would be a category
error, and rewriting the audio engine would be a mistake we have already made
once. From Vaani's own `gateway/pipeline.py`:

    "On real calls that gate threw the caller's voice away while the agent was
     speaking -- the agent talked over the caller and never heard them.
     Real-time audio is not a place to guess."

Pipecat stays the engine. What Vaani owns is the **definition**: which
processors exist, in what order, and why each one sits where it does. That is
the part that decides how the agent behaves, and it is the part that was
previously buried in Dograh's `pipeline_builder.py`.

    Dograh  ->  UI, campaigns, Redis, Postgres, telephony, dashboard
    Pipecat ->  the audio engine, serializers, VAD, turn models
    Vaani   ->  the conversation, and THIS: the shape of the pipeline

Order is load-bearing
---------------------
Every position below was paid for with a real call. Read the notes before
moving anything.
"""

from __future__ import annotations

from loguru import logger

from pipecat.pipeline.pipeline import Pipeline


def vaani_processor_order(
    transport,
    stt,
    audio_buffer,
    llm,
    tts,
    user_context_aggregator,
    assistant_context_aggregator,
    pipeline_engine_callback_processor,
    pipeline_metrics_aggregator,
    *,
    voicemail_detector=None,
    recording_router=None,
    speculation_probe=None,
    speculative_gate=None,
    state_injector=None,
    reply_filter=None,
    partial_responder=None,
    end_call_bridge=None,
    filler_player=None,
) -> list:
    """Return the ordered processor list. Pure — no Pipeline, no side effects.

    Kept separate from :func:`build_vaani_pipeline` so the order can be asserted
    in a test without constructing a real pipeline.
    """
    processors = [
        transport.input(),
        stt,
    ]

    # Speculation, when enabled, must sit HERE and nowhere else: interim
    # transcripts exist only between STT and the aggregator, because
    # LLMContextAggregator consumes InterimTranscriptionFrame and never pushes
    # it downstream. A gate placed after the aggregator sees zero partials and
    # is silently inert -- which is exactly what happened on 2026-08-26.
    # Takes the STT's finalisation off the critical path: when the turn ends
    # before a final transcript arrives, promote the newest partial so the LLM
    # can start. Must sit HERE -- interim frames exist only between STT and the
    # aggregator, which consumes them. Measured cost of not doing this: 1.33s of
    # a 1.91s turn.
    if partial_responder:
        processors.append(partial_responder)

    if speculation_probe:
        processors.append(speculation_probe)

    # Voicemail detection runs after STT but BEFORE the user aggregator, so a
    # classifier verdict cannot trigger a completion on the main context.
    # Deliberately not gated on TTS, so audio keeps flowing while it decides.
    if voicemail_detector:
        logger.info("Adding native voicemail detector to pipeline")
        processors.append(voicemail_detector.detector())

    # Vaani's brain, first half. Runs triage on what the caller just said and
    # rewrites the trailing system message with a fresh state block. Must be
    # BEFORE the user aggregator so the state is current at the moment the LLM
    # fires -- this is what replaces the node graph's question coverage.
    if state_injector:
        processors.append(state_injector)

    processors.append(user_context_aggregator)

    if voicemail_detector:
        processors.append(voicemail_detector.llm_gate())

    # Directly before the LLM so it can swallow the generation trigger on a
    # speculation hit. Pass-through on a miss.
    if speculative_gate:
        processors.append(speculative_gate)

    processors.append(llm)

    # Vaani's brain, second half. Sanitises the reply and gates each chunk
    # against the hard rules before it reaches the speech engine, replacing the
    # reply outright when a blocking rule trips. For a subsidy-linked product,
    # prompt instructions alone are not control.
    #
    # It also sits upstream of the assistant context aggregator, so the cleaned
    # text is what lands in history -- an uncleaned blob taught the next turn
    # the same malformed shape, which is how one bad turn became a run of them.
    if reply_filter:
        processors.append(reply_filter)

    processors.append(pipeline_engine_callback_processor)

    if recording_router:
        processors.append(recording_router)

    processors.append(tts)

    # AFTER tts and immediately before the transport. It plays a short Telugu
    # filler into the silence while the reply is still being produced -- run 262
    # measured 0.921s of endpoint+STT against 0.290s of LLM, so most of every
    # gap is dead air the caller sits through.
    #
    # Here and nowhere else. It must reach the transport's audio queue directly,
    # so the filler and the real reply play in sequence instead of on top of
    # each other, and no ducking or cancellation logic is needed. Placed before
    # `tts` it would have to survive that processor; placed after
    # `transport.output()` it would never be heard at all.
    if filler_player:
        processors.append(filler_player)

    processors.append(transport.output())

    # AFTER transport.output(): BotStoppedSpeakingFrame is emitted by the
    # transport once the audio has actually finished playing, which is the only
    # safe moment to hang up -- earlier and the goodbye is cut off mid-word.
    if end_call_bridge:
        processors.append(end_call_bridge)

    processors.extend(
        [
            audio_buffer,             # records both directions
            assistant_context_aggregator,
            pipeline_metrics_aggregator,
        ]
    )

    return processors


def build_vaani_pipeline(*args, **kwargs) -> Pipeline:
    """Assemble the Vaani pipeline. Signature mirrors :func:`vaani_processor_order`."""
    return Pipeline(vaani_processor_order(*args, **kwargs))
