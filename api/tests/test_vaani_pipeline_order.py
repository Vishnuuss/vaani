"""Locks the order of Vaani's pipeline.

Every position in this list was paid for with a real call, and two features
have already shipped silently inert because a processor sat in the wrong place:

* `SpeculativeLLMGate` after the aggregator -> saw zero interim transcripts,
  because `LLMContextAggregator` consumes them and never pushes them on.
* `StateInjector` after the aggregator -> the state block would be one turn
  stale at the moment the LLM fires.

Neither showed up as a failure. Both looked wired. So the order is asserted
explicitly here rather than trusted to review.
"""

from api.services.vaani.pipeline import vaani_processor_order


class _Transport:
    def __init__(self):
        self._in = "transport.input"
        self._out = "transport.output"

    def input(self):
        return self._in

    def output(self):
        return self._out


class _Voicemail:
    def detector(self):
        return "voicemail.detector"

    def llm_gate(self):
        return "voicemail.llm_gate"


def _order(**overrides):
    kwargs = dict(
        transport=_Transport(),
        stt="stt",
        audio_buffer="audio_buffer",
        llm="llm",
        tts="tts",
        user_context_aggregator="aggregator.user",
        assistant_context_aggregator="aggregator.assistant",
        pipeline_engine_callback_processor="engine_callbacks",
        pipeline_metrics_aggregator="metrics",
    )
    kwargs.update(overrides)
    transport = kwargs.pop("transport")
    return vaani_processor_order(
        transport,
        kwargs.pop("stt"),
        kwargs.pop("audio_buffer"),
        kwargs.pop("llm"),
        kwargs.pop("tts"),
        kwargs.pop("user_context_aggregator"),
        kwargs.pop("assistant_context_aggregator"),
        kwargs.pop("pipeline_engine_callback_processor"),
        kwargs.pop("pipeline_metrics_aggregator"),
        **kwargs,
    )


def test_minimal_pipeline_matches_dograh_baseline():
    """With every optional processor off, the order is Dograh's original."""
    assert _order() == [
        "transport.input",
        "stt",
        "aggregator.user",
        "llm",
        "engine_callbacks",
        "tts",
        "transport.output",
        "audio_buffer",
        "aggregator.assistant",
        "metrics",
    ]


def test_full_pipeline_order_is_exact():
    order = _order(
        voicemail_detector=_Voicemail(),
        recording_router="recording_router",
        speculation_probe="speculation_probe",
        speculative_gate="speculative_gate",
        state_injector="state_injector",
        reply_filter="reply_filter",
    )
    assert order == [
        "transport.input",
        "stt",
        "speculation_probe",      # only place interim transcripts exist
        "voicemail.detector",
        "state_injector",         # BEFORE the aggregator: state must be current
        "aggregator.user",
        "voicemail.llm_gate",
        "speculative_gate",       # must be able to swallow the LLM trigger
        "llm",
        "reply_filter",           # BEFORE tts: nothing unspeakable is spoken
        "engine_callbacks",
        "recording_router",
        "tts",
        "transport.output",
        "audio_buffer",
        "aggregator.assistant",
        "metrics",
    ]


def test_partial_responder_sits_between_stt_and_the_aggregator():
    """Interim frames exist ONLY there -- the aggregator consumes them.

    Anywhere else and it sees zero partials and is silently inert, which is how
    two features already shipped dead this month.
    """
    order = _order(partial_responder="partial_responder")
    assert order.index("stt") < order.index("partial_responder")
    assert order.index("partial_responder") < order.index("aggregator.user")


def test_speculation_probe_precedes_the_user_aggregator():
    """Regression: after the aggregator it sees zero partials and is inert."""
    order = _order(speculation_probe="speculation_probe")
    assert order.index("speculation_probe") < order.index("aggregator.user")


def test_state_injector_precedes_the_user_aggregator():
    """Regression: after it, the state block is a turn stale when the LLM fires."""
    order = _order(state_injector="state_injector")
    assert order.index("state_injector") < order.index("aggregator.user")


def test_end_call_bridge_sits_after_transport_output():
    """BotStoppedSpeaking is emitted by the transport when audio finishes.

    Anywhere earlier and the hangup cuts the goodbye off mid-word.
    """
    order = _order(end_call_bridge="end_call_bridge")
    assert order.index("transport.output") < order.index("end_call_bridge")


def test_reply_filter_precedes_tts():
    """Guardrails are only control if they run before the speech engine."""
    order = _order(reply_filter="reply_filter")
    assert order.index("reply_filter") < order.index("tts")


def test_speculative_gate_precedes_the_llm():
    order = _order(speculative_gate="speculative_gate")
    assert order.index("speculative_gate") < order.index("llm")
