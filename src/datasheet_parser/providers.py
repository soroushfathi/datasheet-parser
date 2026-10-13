"""Interchangeable inference transports with recorded inputs and strict validation."""

import base64
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaError
from pydantic import BaseModel

from .config import Settings, resolve_codex_executable
from .token_budget import request_input_metrics, serialized_prompt

_FOLDER_LOCKS = {}
_FOLDER_LOCKS_GUARD = Lock()


def _folder_lock(path):
    with _FOLDER_LOCKS_GUARD:
        return _FOLDER_LOCKS.setdefault(str(path.resolve()), Lock())


class PendingResponse(RuntimeError):
    pass


class ProviderError(RuntimeError):
    def __init__(self, message, *, code="provider_error", retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def codex_failure(log: str) -> tuple[str, bool]:
    """Classify transport failures without exposing arbitrary server messages or credentials."""
    lowered = log.lower()
    if any(t in lowered for t in ["usage limit", "quota exceeded", "rate limit", "too many requests"]):
        return "usage_or_rate_limit", False
    if any(t in lowered for t in ["unauthorized", "authentication", "invalid api key", "token expired"]):
        return "authentication_failed", False
    if "workspace routing discovery timed out" in lowered:
        return "routing_timeout", True
    if any(
        t in lowered
        for t in [
            "connection reset",
            "connection closed",
            "stream disconnected",
            "request timed out",
            "error sending request",
            "temporarily unavailable",
            "tls handshake eof",
        ]
    ):
        return "transport_error", True
    return "provider_error", False


def strict_schema(model: type[BaseModel]) -> dict:
    """OpenAI/Codex schemas require every object property, including nullable ones."""
    schema = model.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


@dataclass
class LLMRequest:
    task: str
    prompt: str
    schema: dict
    images: list[Path]
    output_budget: int | None = None
    metrics: dict | None = None

    def fingerprint(self, model: str, reasoning: str) -> str:
        data = {
            "task": self.task,
            "prompt": serialized_prompt(self.prompt, self.schema, model),
            "schema": self.schema,
            "image_hashes": [sha256(p.read_bytes()).hexdigest() for p in self.images],
            "model": model,
            "reasoning": reasoning,
        }
        if self.output_budget is not None:
            data["output_budget"] = self.output_budget
        return sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class Provider(Protocol):
    def generate(self, request: LLMRequest) -> dict: ...


class RecordedProvider:
    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.work_dir / "llm"

    def generate(self, request: LLMRequest) -> dict:
        key = request.fingerprint(self.settings.model, self.settings.reasoning)
        # Provider instances are per worker; identical fingerprints still share files.
        # Serialize those writes without treating log reuse as inference caching.
        with _folder_lock(self.root / self.name / key):
            return self._generate(request, key)

    def _generate(self, request: LLMRequest, key: str) -> dict:
        request = replace(request, prompt=serialized_prompt(request.prompt, request.schema, self.settings.model))
        self.last_record = {}
        folder = self.root / self.name / key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "prompt.txt").write_text(request.prompt, encoding="utf-8")
        (folder / "schema.json").write_text(json.dumps(request.schema), encoding="utf-8")
        images = []
        for index, path in enumerate(request.images):
            target = folder / f"image-{index}{path.suffix}"
            if not target.exists():
                shutil.copyfile(path, target)
            images.append(target.resolve())
        (folder / "request.json").write_text(
            json.dumps(
                {
                    "task": request.task,
                    "fingerprint": key,
                    "provider": self.name,
                    "model": self.settings.model,
                    "reasoning": self.settings.reasoning,
                    "images": [p.name for p in images],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        start = time.monotonic()
        request_metrics = {
            "prompt_chars": len(request.prompt),
            "schema_chars": len(json.dumps(request.schema)),
            "image_count": len(images),
            "image_bytes": sum(p.stat().st_size for p in images),
            "output_budget": request.output_budget,
            **(request.metrics or {}),
        }
        from PIL import Image

        dimensions = []
        for path in images:
            try:
                with Image.open(path) as bitmap:
                    dimensions.append({"width": bitmap.width, "height": bitmap.height})
            except OSError:
                dimensions.append({"width": None, "height": None})
        request_metrics["image_dimensions"] = dimensions
        request_metrics.update(request_input_metrics(request.prompt, request.schema, images))
        request_metrics["output_budget_enforcement"] = (
            "api_total_limit" if self.name in {"openai", "openai-chat"} else "prompt_hint"
        )
        (folder / "input_metrics.json").write_text(json.dumps(request_metrics), encoding="utf-8")
        execute_started = False
        try:
            budget = request_metrics.get("request_token_budget")
            if budget and request_metrics["input_tokens_estimate"] > budget:
                raise ProviderError(
                    f"{request.task}: estimated serialized request exceeds {budget}-token budget; "
                    f"inspect {folder / 'input_metrics.json'} before creating a larger-budget run.",
                    code="request_budget_exceeded",
                )
            # Attempts belong to this invocation; a previous fingerprint-folder failure/success
            # must not be counted as a retry when this request stops before inference.
            (folder / "attempts.json").unlink(missing_ok=True)
            execute_started = True
            response, telemetry = self.execute(request, folder, images)
            try:
                Draft202012Validator(request.schema).validate(response)
            except JSONSchemaError:
                # Private audit artifact only; a nonconforming router response is
                # never a successful checkpoint or public progress message.
                (folder / "unvalidated-response.json").write_text(
                    json.dumps(response, ensure_ascii=False), encoding="utf-8")
                raise
            (folder / "response.json").write_text(
                json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            metadata = {
                "provider": self.name,
                "requested_model": self.settings.model,
                "elapsed_seconds": time.monotonic() - start,
                "time": datetime.now(UTC).isoformat(),
                "status": "success",
                "request_metrics": request_metrics,
                "response_bytes": len(json.dumps(response).encode()),
                **telemetry,
            }
            (folder / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            (folder / "failure.json").unlink(missing_ok=True)
            self.last_record = {"folder": str(folder), "fingerprint": key, **metadata}
            return response
        except PendingResponse:
            raise
        except Exception as error:
            metadata = {
                "provider": self.name,
                "requested_model": self.settings.model,
                "status": "failed",
                "type": type(error).__name__,
                "elapsed_seconds": time.monotonic() - start,
                "usage": None,
                "code": getattr(error, "code", "provider_error"),
                "request_metrics": request_metrics,
                "attempts": json.loads((folder / "attempts.json").read_text())
                if execute_started and (folder / "attempts.json").exists()
                else [],
            }
            (folder / "failure.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            (folder / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            self.last_record = {"folder": str(folder), "fingerprint": key, **metadata}
            if isinstance(error, ProviderError):
                raise
            raise ProviderError(f"{self.name} failed ({type(error).__name__}); see {folder}") from error

    def execute(self, request, folder, images):
        raise NotImplementedError


class CodexProvider(RecordedProvider):
    name = "codex"

    def execute(self, request, folder, images):
        attempts = []
        for number in range(1, self.settings.llm_attempts + 1):
            attempt = folder / "attempts" / f"{uuid.uuid4().hex}"
            attempt.mkdir(parents=True)
            started = time.monotonic()
            try:
                response, telemetry = self.execute_once(request, folder, images, attempt)
                attempts.append(
                    {"attempt": number, "status": "success", "elapsed_seconds": time.monotonic() - started}
                )
                return response, {**telemetry, "attempts": attempts}
            except ProviderError as error:
                attempts.append(
                    {
                        "attempt": number,
                        "status": "failed",
                        "code": error.code,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
                if not error.retryable or number == self.settings.llm_attempts:
                    raise
                logging.getLogger(__name__).warning(
                    "Codex %s; retrying attempt %s/%s", error.code, number + 1, self.settings.llm_attempts
                )
                time.sleep(min(2**number, 10))
            finally:
                (folder / "attempts.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")
                for filename in ["events.jsonl", "stderr.log", "answer.json"]:
                    if (attempt / filename).exists():
                        shutil.copyfile(attempt / filename, folder / filename)

    def execute_once(self, request, folder, images, attempt):
        executable = resolve_codex_executable(self.settings)
        if not executable:
            raise ProviderError(
                "Codex CLI not found on PATH. Set DATASHEET_CODEX_EXECUTABLE in .env "
                "to its absolute executable path, or install Codex CLI and sign in."
            )
        answer = attempt / "answer.json"
        # Fresh process, no inherited conversation or user tools/configuration.
        args = [
            executable,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            self.settings.model,
            "-c",
            f'model_reasoning_effort="{self.settings.reasoning}"',
            "-c",
            'web_search="disabled"',
            "-c",
            "features.shell_tool=false",
            "--output-schema",
            str((folder / "schema.json").resolve()),
            "--output-last-message",
            str(answer.resolve()),
            "--json",
            "--color",
            "never",
        ]
        for path in images:
            args.extend(["--image", str(path)])
        args.append("-")
        # Do not give extraction subprocesses our Mongo/API credentials.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("DATASHEET_") and k not in {"OPENAI_API_KEY", "CODEX_API_KEY", "MONGODB_URI"}
        }
        if self.settings.codex_proxy:
            for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
                env[name] = self.settings.codex_proxy
        try:
            with (
                (attempt / "events.jsonl").open("w", encoding="utf-8") as stdout,
                (attempt / "stderr.log").open("w", encoding="utf-8") as stderr,
            ):
                completed = subprocess.run(
                    args,
                    input=request.prompt,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=stdout,
                    stderr=stderr,
                    cwd=folder,
                    env=env,
                    timeout=self.settings.llm_timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as error:
            raise ProviderError(
                f"Codex process exceeded {self.settings.llm_timeout}s; logs: {attempt}",
                code="process_timeout",
                retryable=True,
            ) from error
        output = (attempt / "events.jsonl").read_text(encoding="utf-8", errors="replace")
        stderr_text = (attempt / "stderr.log").read_text(encoding="utf-8", errors="replace")
        telemetry = {"usage": None, "model_reported": None}
        terminal_status = None
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                telemetry["usage"] = event.get("usage")
                terminal_status = "completed"
            if event.get("type") == "turn.failed":
                terminal_status = "failed"
            item = event.get("item", {})
            if item.get("type") in {"command_execution", "web_search", "mcp_tool_call", "file_change"}:
                raise ProviderError("Extraction attempted an external tool; response rejected for isolation")
        # Nonterminal reconnect warnings can recover; only accept a completed terminal event.
        if completed.returncode or terminal_status != "completed" or not answer.exists():
            code, retryable = codex_failure(output + "\n" + stderr_text)
            raise ProviderError(
                f"Codex {code} (exit {completed.returncode}); logs: {attempt}", code=code, retryable=retryable
            )
        return json.loads(answer.read_text(encoding="utf-8-sig")), telemetry


class OpenAIProvider(RecordedProvider):
    name = "openai"

    def execute(self, request, folder, images):
        from openai import OpenAI

        if not os.getenv("OPENAI_API_KEY"):
            raise ProviderError("OPENAI_API_KEY is required only for the openai provider")
        content = [{"type": "input_text", "text": request.prompt}]
        for path in images:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode(),
                }
            )
        response = OpenAI(timeout=self.settings.llm_timeout, max_retries=0).responses.create(
            model=self.settings.model,
            input=[{"role": "user", "content": content}],
            reasoning={"effort": self.settings.reasoning},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "datasheet_result",
                    "strict": True,
                    "schema": request.schema,
                }
            },
            **({"max_output_tokens": request.output_budget} if request.output_budget else {}),
        )
        if response.status != "completed":
            raise ProviderError(
                "Incomplete model response; refusing truncated structured output", code="incomplete_response"
            )
        return json.loads(response.output_text), {
            "usage": response.usage.model_dump() if response.usage else None,
            "model_reported": response.model,
            "response_id": response.id,
        }


class OpenAIChatProvider(RecordedProvider):
    """Explicit Chat Completions transport for OpenAI-compatible gateways.

    OPENAI_BASE_URL and OPENAI_API_KEY stay in the server environment. No silent
    fallback to JSON mode or another model: schema and vision must be supported.
    """

    name = "openai-chat"

    def execute(self, request, folder, images):
        from httpx import TransportError
        from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

        if not os.getenv("OPENAI_API_KEY"):
            raise ProviderError("OPENAI_API_KEY is required for openai-chat")
        content = [{"type": "text", "text": request.prompt}]
        for path in images:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode(),
            }})
        # The section budget describes JSON, while reasoning consumes this API's
        # same total output allowance. Reserve room without relaxing input bounds.
        cap = min(32768, (request.output_budget or 16000) + 8192)
        if self.settings.llm_stage_deadline:
            import asyncio

            from .stream_deadline import execute_stream
            return asyncio.run(execute_stream(self.settings, request, folder, content, cap,
                getattr(self, "progress", lambda *args, **kwargs: None)))
        attempts = []
        with OpenAI(timeout=self.settings.llm_timeout, max_retries=0) as client:
            for number in range(1, self.settings.llm_attempts + 1):
                started = time.monotonic()
                try:
                    stream = client.chat.completions.create(
                        model=self.settings.model,
                        messages=[{"role": "user", "content": content}],
                        reasoning_effort=self.settings.reasoning,
                        response_format={"type": "json_schema", "json_schema": {
                            "name": "datasheet_result", "strict": True, "schema": request.schema,
                        }},
                        max_completion_tokens=cap,
                        stream=True, stream_options={"include_usage": True},
                    )
                    content_parts, finish, usage, refusal = [], None, None, False
                    response_id, reported_model = None, None
                    # Some compatible routers append an SSE terminator even to a
                    # non-streaming JSON response. Use their native SSE transport.
                    with stream:
                        for chunk in stream:
                            response_id, reported_model = chunk.id, chunk.model
                            if chunk.usage:
                                usage = chunk.usage.model_dump()
                            if chunk.choices:
                                choice = chunk.choices[0]
                                if choice.delta.content:
                                    content_parts.append(choice.delta.content)
                                refusal = refusal or bool(choice.delta.refusal)
                                finish = choice.finish_reason or finish
                    if finish != "stop" or refusal or not content_parts:
                        raise ProviderError("Incomplete or refused structured response", code="incomplete_response")
                    attempts.append({"attempt": number, "status": "success",
                                     "elapsed_seconds": time.monotonic() - started})
                    return json.loads("".join(content_parts)), {
                        "usage": usage,
                        "model_reported": reported_model, "response_id": response_id,
                        "api_output_token_cap": cap, "attempts": attempts,
                    }
                except (APIConnectionError, APITimeoutError, APIStatusError, TransportError) as error:
                    status = getattr(error, "status_code", None)
                    retryable = status is None or status in {408, 429} or status >= 500
                    attempts.append({"attempt": number, "status": "failed", "http_status": status,
                                     "elapsed_seconds": time.monotonic() - started})
                    if not retryable or number == self.settings.llm_attempts:
                        raise ProviderError("Gateway request failed", code="gateway_error",
                                            retryable=retryable) from error
                    time.sleep(min(2**number, 10))
                finally:
                    (folder / "attempts.json").write_text(json.dumps(attempts), encoding="utf-8")


class ExternalWorkerProvider(RecordedProvider):
    name = "external"

    def execute(self, request, folder, images):
        path = folder / "submitted.json"
        if not path.exists():
            raise PendingResponse(
                f"Request ready in {folder}. Save worker JSON as submitted.json, then resume."
            )
        return json.loads(path.read_text(encoding="utf-8-sig")), {"human_assisted": True, "usage": None}


class ReplayProvider(RecordedProvider):
    name = "replay"

    def execute(self, request, folder, images):
        key = request.fingerprint(self.settings.model, self.settings.reasoning)
        source = self.root / os.getenv("DATASHEET_REPLAY_SOURCE", "codex") / key / "response.json"
        if not source.exists():
            raise ProviderError(f"No matching recorded response: {source}")
        return json.loads(source.read_text(encoding="utf-8")), {"replayed": True, "usage": None}


def make_provider(settings: Settings) -> RecordedProvider:
    types = {
        "codex": CodexProvider,
        "openai": OpenAIProvider,
        "openai-chat": OpenAIChatProvider,
        "external": ExternalWorkerProvider,
        "replay": ReplayProvider,
    }
    if settings.provider not in types:
        raise ValueError(f"Unknown provider: {settings.provider}")
    return types[settings.provider](settings)
