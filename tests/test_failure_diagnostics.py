"""Safe typed errors differentiate local limits from inference/storage failures."""

import json

import httpx
import pytest
from gridfs.errors import FileExists
from jsonschema.exceptions import ValidationError
from openai import APIStatusError
from pymongo.errors import ServerSelectionTimeoutError

from datasheet_parser.errors import ReviewBudgetError, failure_diagnostics, public_failure_message
from datasheet_parser.providers import ProviderError


@pytest.mark.parametrize("error,code", [
    (ReviewBudgetError(10, 20577, 20000), "review_budget_exceeded"),
    (ProviderError("fixture-secret", code="request_budget_exceeded"), "request_budget_exceeded"),
    (ProviderError("fixture-secret", code="gateway_error"), "gateway_error"),
    (ProviderError("fixture-secret", code="incomplete_response"), "incomplete_response"),
    (ProviderError("fixture-secret", code="fixture-secret"), "provider_error"),
    (FileExists("fixture-secret"), "storage_error"),
    (ServerSelectionTimeoutError("fixture-secret"), "storage_error"),
    (PermissionError("fixture-secret"), "storage_error"),
    (ValidationError("fixture-secret"), "output_validation_error"),
    (ValueError("fixture-secret"), "validation_error"),
    (RuntimeError("fixture-secret"), "internal_error"),
])
def test_allowlisted_failure_diagnostics_never_include_exception_text(error, code):
    failure = failure_diagnostics(error, "review:10")
    assert failure["code"] == code and failure["stage"] == "review:10"
    assert "fixture-secret" not in json.dumps(failure) + public_failure_message(failure)


def test_generic_provider_wrapper_retains_schema_validation_root_cause():
    error = ProviderError("private response body")
    error.__cause__ = ValidationError("private response invalid")
    assert failure_diagnostics(error)["code"] == "output_validation_error"


@pytest.mark.parametrize("status,code", [(401, "authentication_failed"), (403, "authentication_failed"),
                                         (429, "usage_or_rate_limit"), (502, "gateway_error")])
def test_gateway_http_status_is_safe_and_actionable(status, code):
    cause = APIStatusError("fixture-secret gateway body", response=httpx.Response(status,
                           request=httpx.Request("GET", "https://fixture/private")), body={"key": "fixture-secret"})
    error = ProviderError("fixture-secret", code="gateway_error", retryable=True)
    error.__cause__ = cause
    failure = failure_diagnostics(error)
    assert failure["code"] == code and failure["details"]["http_status"] == status
    assert "fixture-secret" not in json.dumps(failure)


def test_safe_trace_records_source_locations_without_source_or_absolute_paths():
    from datasheet_parser.engine import Engine

    engine = Engine(None, None, None)
    try:
        engine.pages("fixture-private-id")
    except AttributeError as error:
        failure = failure_diagnostics(error)
    frames = failure["source_frames"]
    assert any(frame["file"] == "engine.py" and frame["function"] == "pages" for frame in frames)
    assert all(set(frame) == {"file", "function", "line"} for frame in frames)
    assert "fixture-private-id" not in json.dumps(failure)


def test_source_bundle_limit_message_does_not_claim_request_token_overflow():
    diagnostic = failure_diagnostics(ReviewBudgetError(10, 15000, 20000, "cited_evidence_bundle"))
    message = public_failure_message(diagnostic)
    assert "متن یا تصویر" in message
    assert "15,000" not in message and "20,000" not in message
