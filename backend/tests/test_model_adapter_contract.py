import httpx
import pytest

from app.domain.runtime import LLMRuntimeConfig
from app.runtime.models import ModelRuntimeError, OpenAICompatibleAdapter


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "prompt"])
async def test_structured_modes_validate_output_and_keep_usage_unknown(monkeypatch, mode):
    seen = []

    async def reply(request):
        import json

        seen.append(json.loads(request.content))
        assert str(request.url) == "https://provider.invalid/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"value":3}'}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs),
    )
    monkeypatch.setenv("TEST_MODEL_KEY", "test")
    adapter = OpenAICompatibleAdapter(
        LLMRuntimeConfig(
            "openai_compatible", "https://provider.invalid/v1", "test-model", "TEST_MODEL_KEY"
        ),
        structured_mode=mode,
    )
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    result = await adapter.generate("extract", schema, 100)
    assert result.output == {"value": 3}
    assert result.usage_complete is False
    if mode == "prompt":
        assert "response_format" not in seen[0]
    else:
        assert seen[0]["response_format"]["type"] == mode
    with pytest.raises(ModelRuntimeError, match="输出契约"):
        await adapter.generate("extract", {"type": "object", "required": ["missing"]}, 100)


async def test_text_stream_has_deltas_and_usage_without_agent(monkeypatch):
    async def reply(request):
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs),
    )
    monkeypatch.setenv("TEST_MODEL_KEY", "test")
    adapter = OpenAICompatibleAdapter(
        LLMRuntimeConfig(
            "openai_compatible", "https://provider.invalid/v1", "test-model", "TEST_MODEL_KEY"
        )
    )
    events = [event async for event in adapter.stream_text([{"role": "user", "content": "hello"}])]
    assert "".join(event.delta for event in events) == "Hello"
    assert events[-1].input_tokens == 3
    assert events[-1].output_tokens == 1
