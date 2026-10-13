import asyncio
import json
import time

import httpx
import pytest

from datasheet_parser.config import Settings
from datasheet_parser.providers import LLMRequest, OpenAIChatProvider, ProviderError


def test_sse_heartbeats_do_not_extend_wall_deadline_and_stream_closes(tmp_path,monkeypatch):
    from openai import AsyncOpenAI

    import datasheet_parser.stream_deadline as transport
    closed=[]
    class Heartbeats(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b": heartbeat\n\n"
        async def aclose(self):
            closed.append(True)
    async def handler(req):
        return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=Heartbeats())
    monkeypatch.setenv("OPENAI_API_KEY","fixture-secret")
    client=AsyncOpenAI(api_key="fixture-secret",max_retries=0,
        base_url="https://fixture/v1",http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    _ = client.chat.completions  # Initialize lazy SDK resources before timing the stream.
    monkeypatch.setattr(transport,"AsyncOpenAI",lambda **kwargs: client)
    # Isolate Windows platform probing from the network stream under test.
    monkeypatch.setattr("openai._base_client.get_platform",lambda:"Windows")
    monkeypatch.setattr("openai._base_client.get_architecture",lambda:"x64")
    settings=Settings(mongo_uri="unused",work_dir=tmp_path,llm_stage_deadline=.8,
                      llm_attempt_deadline=1,llm_attempts=2)
    provider=OpenAIChatProvider(settings)
    request=LLMRequest("smoke","return ok",{"type":"object","properties":{
        "ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":False},[])
    began=time.monotonic()
    with pytest.raises(ProviderError) as caught:
        provider.generate(request)
    assert caught.value.code == "request_deadline_exceeded"
    from datasheet_parser.errors import failure_diagnostics
    assert failure_diagnostics(caught.value)["code"] == "request_deadline_exceeded"
    assert time.monotonic()-began < 2
    assert closed
    attempts=json.loads(next((tmp_path/"llm").rglob("attempts.json")).read_text())
    assert attempts[-1]["reason"]=="stage_deadline"


@pytest.mark.parametrize("code,recovers", [("gateway_timeout",True),("unknown_error",False)])
def test_in_band_sse_gateway_error_retries_without_relaxing_schema(tmp_path,monkeypatch,code,recovers):
    from openai import AsyncOpenAI

    import datasheet_parser.stream_deadline as transport
    calls=[]
    async def handler(req):
        calls.append(req)
        if len(calls)==1:
            event={"error":{"message":"fixture-private-upstream-message","code":code}}
        else:
            event={"id":"fixture","object":"chat.completion.chunk","created":1,"model":"fixture",
                   "choices":[{"index":0,"delta":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}
        return httpx.Response(200,headers={"content-type":"text/event-stream"},
                              content="data: "+json.dumps(event)+"\n\ndata: [DONE]\n\n")
    monkeypatch.setenv("OPENAI_API_KEY","fixture-secret")
    client=AsyncOpenAI(api_key="fixture-secret",base_url="https://fixture/v1",max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(transport,"AsyncOpenAI",lambda **kwargs:client)
    settings=Settings(mongo_uri="unused",work_dir=tmp_path,llm_stage_deadline=10,
                      llm_attempt_deadline=10,llm_attempts=2)
    provider=OpenAIChatProvider(settings)
    request=LLMRequest("extract:9","Return only structured source facts",{"type":"object",
        "properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":False},[])
    if recovers:
        assert provider.generate(request)=={"ok":True}
        assert len(calls)==2
        assert provider.last_record["attempts"][0]["upstream_code"]==code
        assert provider.last_record["attempts"][0]["http_status"] is None
        assert provider.last_record["attempts"][0]["reason"]=="stream_gateway_error"
    else:
        with pytest.raises(ProviderError) as caught:
            provider.generate(request)
        assert not caught.value.retryable and len(calls)==1
    assert "fixture-private-upstream-message" not in json.dumps(provider.last_record)
    assert all(json.loads(call.content)["response_format"]["json_schema"]["strict"] for call in calls)
