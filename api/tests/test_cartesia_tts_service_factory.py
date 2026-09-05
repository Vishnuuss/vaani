from types import SimpleNamespace
from unittest.mock import patch

from api.services.configuration.registry import (
    CARTESIA_TTS_MODELS,
    CartesiaTTSConfiguration,
    ServiceProviders,
)
from api.services.pipecat.service_factory import create_tts_service


def test_cartesia_tts_configuration_defaults_to_sonic_3_5():
    config = CartesiaTTSConfiguration(api_key="test-key")

    assert config.provider == ServiceProviders.CARTESIA
    assert config.model == "sonic-3.5"
    assert CARTESIA_TTS_MODELS == ["sonic-3.5", "sonic-3"]


def test_create_cartesia_tts_service_passes_selected_model():
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.CARTESIA.value,
            api_key="test-key",
            model="sonic-3.5",
            voice="test-voice-id",
            speed=1.0,
            volume=1.0,
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=24000,
        transport_in_sample_rate=16000,
    )

    with patch(
        "api.services.pipecat.service_factory.CartesiaTTSService"
    ) as mock_service:
        create_tts_service(user_config, audio_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["api_key"] == "test-key"
    assert kwargs["settings"].model == "sonic-3.5"
    assert kwargs["settings"].voice == "test-voice-id"


def test_cartesia_tts_configuration_default_language_is_english():
    config = CartesiaTTSConfiguration(api_key="test-key")

    assert config.language == "en"


def test_create_cartesia_tts_service_passes_language_to_settings():
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.CARTESIA.value,
            api_key="test-key",
            model="sonic-3.5",
            voice="test-voice-id",
            speed=1.0,
            volume=1.0,
            language="tr",
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=24000,
        transport_in_sample_rate=16000,
    )

    with patch(
        "api.services.pipecat.service_factory.CartesiaTTSService"
    ) as mock_service:
        create_tts_service(user_config, audio_config)

    kwargs = mock_service.call_args.kwargs
    assert kwargs["settings"].language == "tr"


# --- token streaming, 5 Sep -------------------------------------------------
#
# The client: "it should speak while generating only not after complete".
#
# He was right and the code was not doing it. `service_factory` passes
# `text_aggregation_mode=TextAggregationMode.TOKEN` to DEEPGRAM, with a comment
# whose reasoning is written about CARTESIA -- "Cartesia is a websocket service
# and accepts incremental text" -- and the Cartesia branch never got it.
#
# pipecat's own Cartesia docstring: "we aggregate sentences before sending to
# TTS. This adds ~200-300ms of latency per sentence ... TODO: Consider making
# TOKEN the default for Cartesia in 1.0."
#
# Worse for this agent than the generic 200-300ms: `SimpleTextAggregator` will
# not release a sentence until a NON-WHITESPACE character arrives after its
# terminal punctuation, and Vaani replies are typically one sentence ending in a
# question mark. That character never comes, so the sentence is released only by
# the flush on LLMFullResponseEndFrame -- i.e. the TTS receives nothing at all
# until the LLM has finished generating the entire reply.
#
# Opt-in per workflow rather than on by default: no test can judge Telugu
# prosody, so this ships behind a flag and is heard before it is trusted.

from pipecat.services.tts_service import TextAggregationMode  # noqa: E402


def _cartesia_config():
    return SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.CARTESIA.value,
            api_key="test-key",
            model="sonic-3",
            voice="test-voice-id",
            language="te",
            speed=1.0,
            volume=1.0,
        )
    )


def _audio():
    return SimpleNamespace(
        transport_out_sample_rate=24000,
        transport_in_sample_rate=16000,
    )


def test_token_streaming_is_on_by_default():
    """Verified on a real call (run 776) and confirmed by the client, so it is
    the default -- every agent built from here gets it without being told."""
    with patch(
        "api.services.pipecat.service_factory.CartesiaTTSService"
    ) as mock_service:
        create_tts_service(_cartesia_config(), _audio())

    kwargs = mock_service.call_args.kwargs
    assert kwargs["text_aggregation_mode"] is TextAggregationMode.TOKEN


def test_token_streaming_can_still_be_turned_off():
    """One config write must be able to undo it on any single agent."""
    with patch(
        "api.services.pipecat.service_factory.CartesiaTTSService"
    ) as mock_service:
        create_tts_service(_cartesia_config(), _audio(), stream_tokens=False)

    kwargs = mock_service.call_args.kwargs
    assert kwargs.get("text_aggregation_mode") is None


def test_token_streaming_is_passed_when_enabled():
    with patch(
        "api.services.pipecat.service_factory.CartesiaTTSService"
    ) as mock_service:
        create_tts_service(_cartesia_config(), _audio(), stream_tokens=True)

    kwargs = mock_service.call_args.kwargs
    assert kwargs["text_aggregation_mode"] is TextAggregationMode.TOKEN


def test_the_existing_two_argument_call_still_works():
    """Every other caller in the tree passes two positional args and no flag."""
    with patch("api.services.pipecat.service_factory.CartesiaTTSService"):
        create_tts_service(_cartesia_config(), _audio())
