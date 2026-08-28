import os

from loguru import logger

from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.speculation.probe import SpeculationProbe
from api.services.vaani.pipeline import build_vaani_pipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.utils.run_context import turn_var


def create_pipeline_components(audio_config: AudioConfig):
    """Create and return the main pipeline components with proper audio configuration"""
    logger.info(f"Creating pipeline components with audio config: {audio_config}")

    # Use native AudioBufferProcessor for merged audio recording
    audio_buffer = AudioBufferProcessor(
        sample_rate=audio_config.pipeline_sample_rate,
        buffer_size=audio_config.buffer_size_bytes,
    )

    context = LLMContext()

    return audio_buffer, context


def build_pipeline(
    transport,
    stt,
    audio_buffer,
    llm,
    tts,
    user_context_aggregator,
    assistant_context_aggregator,
    pipeline_engine_callback_processor,
    pipeline_metrics_aggregator,
    voicemail_detector=None,
    recording_router=None,
    speculation_probe=None,
    speculative_gate=None,
    state_injector=None,
    reply_filter=None,
    filler_player=None,
    partial_responder=None,
    end_call_bridge=None,
):
    """Build the main pipeline.

    The processor ORDER is not defined here -- it belongs to Vaani, in
    ``api/services/vaani/pipeline.py``. Dograh owns the transport, campaigns,
    Redis, Postgres, telephony and the dashboard; Vaani owns the conversation
    and the shape of the pipeline that carries it. This function stays as the
    call site Dograh already knows about, and forwards.
    """
    if speculation_probe is None:
        speculation_probe = SpeculationProbe()

    return build_vaani_pipeline(
        transport,
        stt,
        audio_buffer,
        llm,
        tts,
        user_context_aggregator,
        assistant_context_aggregator,
        pipeline_engine_callback_processor,
        pipeline_metrics_aggregator,
        voicemail_detector=voicemail_detector,
        recording_router=recording_router,
        speculation_probe=speculation_probe,
        speculative_gate=speculative_gate,
        state_injector=state_injector,
        reply_filter=reply_filter,
        filler_player=filler_player,
        partial_responder=partial_responder,
        end_call_bridge=end_call_bridge,
    )


def build_realtime_pipeline(
    transport,
    realtime_llm,
    audio_buffer,
    user_context_aggregator,
    assistant_context_aggregator,
    pipeline_engine_callback_processor,
    pipeline_metrics_aggregator,
    voicemail_detector=None,
):
    """Build a pipeline for realtime (speech-to-speech) LLM services.

    Realtime services (e.g. OpenAI Realtime, Gemini Live) handle STT+LLM+TTS
    internally, so no separate STT or TTS processors are needed.

    Args:
        voicemail_detector: Optional VoicemailDetector. Placed *below* the
            realtime LLM. This is asymmetric with the non-realtime layout
            (where the detector sits between STT and the main user aggregator)
            because the realtime LLM is both the source of TranscriptionFrame
            (broadcast downstream) and the sink of LLMContextFrame (consumed
            by _handle_context without forwarding). Placing the detector below
            the realtime LLM means: downstream TranscriptionFrames reach the
            classifier branch, UserStartedSpeakingFrame /
            UserStoppedSpeakingFrame are forwarded through by the LLM, and the
            main aggregator's LLMContextFrame is absorbed by the realtime LLM
            and never leaks into the classifier (which would otherwise run a
            voicemail completion on the workflow's main context).

            The TTS gate and LLM gate are intentionally not used: the realtime
            LLM reacts to audio directly, not to LLMContextFrames. On voicemail
            detection we drop the call via end_call_with_reason; the detector's
            ConversationGate also blocks downstream audio output until the call
            ends.
    """
    processors = [
        transport.input(),
        user_context_aggregator,
        realtime_llm,
    ]

    if voicemail_detector:
        logger.info("Adding native voicemail detector to realtime pipeline")
        processors.append(voicemail_detector.detector())

    processors.extend(
        [
            pipeline_engine_callback_processor,
            transport.output(),
            audio_buffer,
            assistant_context_aggregator,
            pipeline_metrics_aggregator,
        ]
    )

    return Pipeline(processors)


def create_pipeline_task(
    pipeline,
    workflow_run_id,
    audio_config: AudioConfig = None,
    *,
    conversation_parent_context=None,
    conversation_type: str = "voice",
    additional_span_attributes: dict | None = None,
):
    """Create a pipeline task with appropriate parameters.

    Args:
        pipeline: The pipeline to run.
        workflow_run_id: Run id, used as the conversation id.
        audio_config: Optional audio configuration.
        conversation_parent_context: Optional OTEL context carrying a fixed
            trace id. When provided, the conversation span attaches to that
            trace instead of starting a new root trace (used by text chat to
            stitch every per-turn pipeline into one trace).
        conversation_type: ``conversation.type`` span attribute value.
        additional_span_attributes: Extra attributes set on the conversation
            span (e.g. ``langfuse.trace.name`` to name a stitched trace that
            has no real root span).
    """
    # Set up pipeline params with audio configuration if provided
    pipeline_params = PipelineParams(
        enable_metrics=True,
        enable_usage_metrics=True,
        send_initial_empty_metrics=False,
        enable_heartbeats=True,
        start_metadata={"workflow_run_id": workflow_run_id},
    )

    # If audio_config is provided, set the audio sample rates
    if audio_config:
        pipeline_params.audio_in_sample_rate = audio_config.transport_in_sample_rate
        pipeline_params.audio_out_sample_rate = audio_config.transport_out_sample_rate
        logger.debug(
            f"Setting pipeline audio params - in: {audio_config.transport_in_sample_rate}Hz, "
            f"out: {audio_config.transport_out_sample_rate}Hz"
        )

    task = PipelineWorker(
        pipeline,
        params=pipeline_params,
        enable_tracing=True,
        enable_rtvi=False,
        conversation_id=f"{workflow_run_id}",
        conversation_parent_context=conversation_parent_context,
        conversation_type=conversation_type,
        additional_span_attributes=additional_span_attributes,
    )

    # Check if turn logging is enabled
    enable_turn_logging = os.getenv("ENABLE_TURN_LOGGING", "false").lower() == "true"

    if enable_turn_logging:
        # Attach event handlers to propagate turn information into the logging context
        turn_observer = task.turn_tracking_observer

        if turn_observer is not None:
            # Import turn context manager only if needed
            from api.services.pipecat.turn_context import get_turn_context_manager

            async def _on_turn_started(observer, turn_number: int):
                """Set the current turn number into the context variable."""
                # Set in both contextvar and turn context manager
                turn_var.set(turn_number)
                turn_manager = get_turn_context_manager()
                turn_manager.set_turn(turn_number)

            # Register the handlers with the observer
            turn_observer.add_event_handler("on_turn_started", _on_turn_started)

    return task
