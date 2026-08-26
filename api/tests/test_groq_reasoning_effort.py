"""Groq reasoning models must be told to stop thinking so much.

`openai/gpt-oss-*` are reasoning models. At Groq's default effort they spend
their first tokens reasoning, which lands squarely on the caller's ear.

Measured directly against the configured key and model, ~2,888-token prompt,
from Indian broadband:

    reasoning_effort=low        TTFT p50 =  631 ms   (5/5 produced speech)
    no reasoning_effort         TTFT p50 = 1699 ms   (2/5 produced speech)

Three of five requests produced NO speech at all inside 60 tokens without it —
the model was still thinking. Dograh already does this for GPT-5 on OpenAI
(`reasoning_effort: minimal`) but set nothing for Groq.

Non-reasoning Groq models must NOT get the parameter — they reject it.
"""

from api.services.pipecat.service_factory import _groq_llm_extra


def test_gpt_oss_models_get_low_reasoning_effort():
    assert _groq_llm_extra("openai/gpt-oss-120b") == {"reasoning_effort": "low"}
    assert _groq_llm_extra("openai/gpt-oss-20b") == {"reasoning_effort": "low"}


def test_a_non_reasoning_model_is_left_alone():
    # Sending reasoning_effort to a model that doesn't support it is an error.
    assert _groq_llm_extra("llama-3.3-70b-versatile") == {}


def test_qwen_reasoning_models_are_covered_too():
    assert _groq_llm_extra("qwen/qwen3-32b") == {"reasoning_effort": "low"}


def test_it_is_case_insensitive():
    assert _groq_llm_extra("OpenAI/GPT-OSS-120B") == {"reasoning_effort": "low"}
