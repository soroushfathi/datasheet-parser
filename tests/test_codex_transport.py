import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from datasheet_parser.config import Settings
from datasheet_parser.providers import CodexProvider, LLMRequest, ProviderError


def transport(tmp_path, monkeypatch, outcomes, **settings):
    calls = []
    monkeypatch.setattr("datasheet_parser.providers.shutil.which", lambda _: "codex.exe")
    monkeypatch.setattr("datasheet_parser.providers.time.sleep", lambda _: None)

    def execute(args, **kwargs):
        calls.append({**kwargs, "argv": args})
        outcome = outcomes[len(calls) - 1]
        kwargs["stdout"].write("\n".join(json.dumps(e) for e in outcome["events"]))
        kwargs["stderr"].write("diagnostic log")
        if outcome.get("timeout"):
            raise subprocess.TimeoutExpired(args, 600)
        if "answer" in outcome:
            Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(outcome["answer"]))
        return SimpleNamespace(returncode=outcome.get("exit", 0))

    monkeypatch.setattr("datasheet_parser.providers.subprocess.run", execute)
    provider = CodexProvider(Settings(mongo_uri="unused", work_dir=tmp_path, **settings))
    request = LLMRequest(
        "test",
        "Read the supplied evidence",
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        [],
    )
    return provider, request, calls


SUCCESS = {"events": [{"type": "turn.completed", "usage": {"output_tokens": 3}}], "answer": {"ok": True}}


def test_recovered_reconnect_is_not_a_failed_turn(tmp_path, monkeypatch):
    outcome = {**SUCCESS, "events": [{"type": "error", "message": "Reconnecting..."}, *SUCCESS["events"]]}
    provider, request, calls = transport(tmp_path, monkeypatch, [outcome])
    assert provider.generate(request) == {"ok": True}
    assert len(calls) == 1


def test_routing_failure_retries_and_keeps_both_attempt_logs(tmp_path, monkeypatch):
    failure = {
        "exit": 1,
        "events": [{"type": "turn.failed", "error": {"message": "workspace routing discovery timed out"}}],
    }
    provider, request, calls = transport(tmp_path, monkeypatch, [failure, SUCCESS])
    assert provider.generate(request)["ok"]
    assert len(calls) == 2
    folder = Path(provider.last_record["folder"])
    assert len(list((folder / "attempts").glob("*/events.jsonl"))) == 2
    assert provider.last_record["attempts"][0]["code"] == "routing_timeout"


def test_usage_limit_is_not_retried(tmp_path, monkeypatch):
    failure = {"exit": 1, "events": [{"type": "turn.failed", "error": {"message": "Usage limit reached"}}]}
    provider, request, calls = transport(tmp_path, monkeypatch, [failure])
    with pytest.raises(ProviderError, match="usage_or_rate_limit"):
        provider.generate(request)
    assert len(calls) == 1


def test_proxy_only_applies_to_child_and_db_secrets_are_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATASHEET_MONGO_URI", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    provider, request, calls = transport(
        tmp_path, monkeypatch, [SUCCESS], codex_proxy="http://127.0.0.1:2080"
    )
    provider.generate(request)
    env = calls[0]["env"]
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:2080"
    assert "DATASHEET_MONGO_URI" not in env
    assert "OPENAI_API_KEY" not in env


def test_timeout_preserves_streamed_logs(tmp_path, monkeypatch):
    provider, request, _ = transport(
        tmp_path, monkeypatch, [{"events": [{"type": "turn.started"}], "timeout": True}], llm_attempts=1
    )
    with pytest.raises(ProviderError, match="exceeded"):
        provider.generate(request)
    folder = Path(provider.last_record["folder"])
    assert "turn.started" in (folder / "events.jsonl").read_text()
    assert provider.last_record["code"] == "process_timeout"


def test_provider_works_without_codex_on_path_when_explicitly_configured(tmp_path, monkeypatch):
    executable = str(tmp_path / "desktop-runtime" / "codex.exe")
    provider, request, calls = transport(tmp_path, monkeypatch, [SUCCESS], codex_executable=executable)
    monkeypatch.setattr(
        "datasheet_parser.config.shutil.which", lambda name: executable if name == executable else None
    )
    assert provider.generate(request) == {"ok": True}
    assert calls[0]["argv"][0] == executable


def test_stale_explicit_executable_does_not_silently_use_other_codex(tmp_path, monkeypatch):
    provider, request, calls = transport(tmp_path, monkeypatch, [], codex_executable="missing.exe")
    monkeypatch.setattr(
        "datasheet_parser.config.shutil.which", lambda name: "other-codex.exe" if name == "codex" else None
    )
    with pytest.raises(ProviderError):
        provider.generate(request)
    assert not calls


def test_answer_without_completed_event_is_not_accepted(tmp_path, monkeypatch):
    provider, request, _ = transport(tmp_path, monkeypatch, [{"events": [], "answer": {"ok": True}}])
    with pytest.raises(ProviderError):
        provider.generate(request)


def test_tool_use_rejected_even_after_a_completed_turn(tmp_path, monkeypatch):
    outcome = {
        **SUCCESS,
        "events": [{"type": "item.completed", "item": {"type": "file_change"}}, *SUCCESS["events"]],
    }
    provider, request, calls = transport(tmp_path, monkeypatch, [outcome])
    with pytest.raises(ProviderError, match="external tool"):
        provider.generate(request)
    assert len(calls) == 1
