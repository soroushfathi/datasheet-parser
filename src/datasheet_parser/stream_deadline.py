"""Cancellable wall deadlines include SSE heartbeats, retries and backoff."""

import asyncio
import json
import time

import httpx
from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI


async def execute_stream(settings, request, folder, content, cap, progress):
    from .providers import ProviderError

    start = time.monotonic()
    attempts = []
    limit = min(settings.llm_stage_deadline,
                (request.metrics or {}).get("wall_deadline_seconds", settings.llm_stage_deadline))
    # A quiet reasoning phase or slow upload can be legitimate. The absolute
    # async deadlines enforce the wall cap; phase limits need not kill it early.
    phase = min(settings.llm_timeout, settings.llm_attempt_deadline)
    timeout = httpx.Timeout(connect=min(30,phase), read=phase, write=phase, pool=15)
    try:
        async with asyncio.timeout(limit):
            async with AsyncOpenAI(timeout=timeout, max_retries=0) as client:
                for number in range(1, settings.llm_attempts + 1):
                    began = time.monotonic()
                    remaining = limit - (began - start)
                    first_event = first_content = None
                    try:
                        progress("attempt_started", attempt=number, remaining_seconds=round(remaining, 1))
                        async with asyncio.timeout(min(remaining, settings.llm_attempt_deadline)):
                            stream = await client.chat.completions.create(
                                model=settings.model, messages=[{"role":"user", "content":content}],
                                reasoning_effort=settings.reasoning,
                                response_format={"type":"json_schema", "json_schema":{
                                    "name":"datasheet_result", "strict":True, "schema":request.schema}},
                                max_completion_tokens=cap, stream=True, stream_options={"include_usage":True})
                            parts, finish, usage, refusal = [], None, None, False
                            response_id = reported_model = None
                            async with stream:
                                async for chunk in stream:
                                    elapsed = time.monotonic() - began
                                    if first_event is None:
                                        first_event = elapsed
                                        progress("first_event", attempt=number, elapsed_seconds=round(elapsed, 1))
                                    response_id, reported_model = chunk.id, chunk.model
                                    if chunk.usage:
                                        usage = chunk.usage.model_dump()
                                    if chunk.choices:
                                        choice = chunk.choices[0]
                                        if choice.delta.content:
                                            if first_content is None:
                                                first_content = elapsed
                                                progress("first_content", attempt=number, elapsed_seconds=round(elapsed, 1))
                                            parts.append(choice.delta.content)
                                        refusal = refusal or bool(choice.delta.refusal)
                                        finish = choice.finish_reason or finish
                            if finish != "stop" or refusal or not parts:
                                raise ProviderError("Incomplete or refused structured response", code="incomplete_response")
                            response = json.loads("".join(parts))
                        attempts.append({"attempt":number, "status":"success",
                            "elapsed_seconds":time.monotonic()-began,
                            "first_event_seconds":first_event, "first_content_seconds":first_content})
                        return response, {"usage":usage,"model_reported":reported_model,
                            "response_id":response_id,"api_output_token_cap":cap,"attempts":attempts,
                            "wall_deadline_seconds":limit}
                    except (TimeoutError, APIError, httpx.TransportError) as error:
                        status = getattr(error,"status_code",None)
                        deadline = isinstance(error,TimeoutError)
                        upstream_code = getattr(error,"code",None)
                        stream_gateway = upstream_code in {"gateway_timeout","server_error","overloaded_error"}
                        transport = isinstance(error,(TimeoutError,APIConnectionError,APITimeoutError,httpx.TransportError))
                        retryable = transport or stream_gateway or status in {408,429} or (status is not None and status >= 500)
                        attempts.append({"attempt":number,"status":"failed","http_status":status,
                            "reason":"attempt_deadline" if deadline else "stream_gateway_error" if stream_gateway else "transport_error",
                            "error_type":type(error).__name__,
                            "cause_type":type(error.__cause__).__name__ if error.__cause__ else None,
                            "elapsed_seconds":time.monotonic()-began,
                            "first_event_seconds":first_event,"first_content_seconds":first_content})
                        if stream_gateway:
                            attempts[-1]["upstream_code"] = upstream_code
                        progress("attempt_failed",attempt=number,reason=attempts[-1]["reason"])
                        if not retryable or number == settings.llm_attempts:
                            raise ProviderError("Bounded request failed", code="request_deadline_exceeded" if deadline else "gateway_error" if retryable else "provider_error",
                                                retryable=retryable) from error
                        await asyncio.sleep(min(2**number,10))
                    finally:
                        (folder/"attempts.json").write_text(json.dumps(attempts),encoding="utf-8")
    except TimeoutError as error:
        # The outer timeout cancels the active async transport and closes its stream/client.
        attempts.append({"attempt":len(attempts)+1,"status":"failed","reason":"stage_deadline",
                         "elapsed_seconds":time.monotonic()-start})
        (folder/"attempts.json").write_text(json.dumps(attempts),encoding="utf-8")
        progress("deadline_exceeded",deadline_seconds=limit)
        raise ProviderError("Total request wall deadline exceeded",code="request_deadline_exceeded",retryable=True) from error
