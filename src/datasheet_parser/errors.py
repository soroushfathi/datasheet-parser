"""Typed failures and allowlisted diagnostics without upstream text or secrets."""

import re
import traceback
from json import JSONDecodeError
from pathlib import Path

import httpx
from gridfs.errors import GridFSError
from jsonschema.exceptions import ValidationError as SchemaValidationError
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from .providers import ProviderError


class ReviewBudgetError(ValueError):
    """An indivisible, source-grounded review still cannot fit its frozen budget."""

    code = "review_budget_exceeded"

    def __init__(self, section_id: int, estimate: int, limit: int, reason="irreducible_review_input"):
        self.section_id, self.estimate, self.limit = int(section_id), int(estimate), int(limit)
        # Reasons are an internal enum, never arbitrary source or exception content.
        self.reason = reason if reason in {"irreducible_review_input", "cited_evidence_bundle", "indivisible_requirement"} else "irreducible_review_input"
        super().__init__(f"Review {self.section_id}: estimated input {self.estimate} exceeds limit {self.limit}")


def safe_stage(stage):
    return stage if isinstance(stage, str) and re.fullmatch(r"[a-z][a-z0-9_-]*(?::[a-z0-9_-]+){0,4}", stage) else None


def _chain(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        chain.append(error)
        seen.add(id(error))
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return chain


def failure_diagnostics(error, stage=None):
    """Describe failure types and numeric facts; never serialize exception messages."""
    chain = _chain(error)
    code, details = "internal_error", {}
    budget = next((e for e in chain if isinstance(e, ReviewBudgetError)), None)
    provider = next((e for e in chain if isinstance(e, ProviderError)), None)
    status_error = next((e for e in chain if isinstance(e, APIStatusError)), None)
    network_failure = (any(isinstance(e, (APIConnectionError, APITimeoutError, httpx.TransportError))
                           for e in chain) or
                       bool(provider and provider.code in {"gateway_error", "transport_error", "routing_timeout"}))
    if budget:
        code = budget.code
        details = {"section_id": budget.section_id, "input_tokens_estimate": budget.estimate,
                   "request_token_budget": budget.limit, "reason": budget.reason}
        stage = stage or f"review:{budget.section_id}"
    elif any(isinstance(e, (PyMongoError, GridFSError)) or
             (isinstance(e, OSError) and not network_failure)
             for e in chain):
        code = "storage_error"
    elif any(isinstance(e, (ValidationError, SchemaValidationError, JSONDecodeError)) for e in chain):
        code = "output_validation_error"
    elif provider:
        allowed = {"provider_error", "request_budget_exceeded", "process_timeout", "gateway_error",
                   "incomplete_response", "usage_or_rate_limit", "authentication_failed", "routing_timeout",
                   "transport_error"}
        code = provider.code if provider.code in allowed else "provider_error"
        details["retryable"] = bool(provider.retryable)
    elif status_error or network_failure:
        code = "gateway_error"
    elif any(isinstance(e, ValueError) for e in chain):
        code = "validation_error"
    if status_error:
        details["http_status"] = int(status_error.status_code)
        if status_error.status_code in {401, 403}:
            code = "authentication_failed"
        elif status_error.status_code == 429:
            code = "usage_or_rate_limit"
    frames = []
    for cause in chain:
        for frame in traceback.extract_tb(cause.__traceback__):
            # No absolute paths, locals, source lines, prompts or exception text.
            if "datasheet_parser" in Path(frame.filename).parts:
                frames.append({"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno})
    return {"code": code, "stage": safe_stage(stage or getattr(error, "_datasheet_stage", None)),
            "details": details, "error_type": type(error).__name__, "source_frames": frames[-12:]}


def public_failure_message(failure):
    details, code = failure["details"], failure["code"]
    stage = failure.get("stage")
    prefix = f"مرحلهٔ {stage}: " if stage else ""
    messages = {
        "request_budget_exceeded": "ورودی درخواست از سقف توکن تنظیم‌شده در برنامه بیشتر است؛ این خطا قبل از تماس با مدل رخ داده است.",
        "storage_error": "ذخیره یا خواندن داده‌ها ناموفق بود؛ اتصال پایگاه داده و فایل‌های سرور باید بررسی شود.",
        "output_validation_error": "پاسخ مدل با قالب یا قواعد اعتبارسنجی خروجی سازگار نبود؛ پاسخ نامعتبر به‌عنوان خروجی نهایی پذیرفته نشد.",
        "validation_error": "دادهٔ سند یا خروجی مرحله از کنترل اعتبارسنجی عبور نکرد؛ شناسهٔ اجرا را برای بررسی ارسال کن.",
        "authentication_failed": "پرووایدر دسترسی را رد کرد؛ تنظیمات کلید و مجوز مدل سمت سرور باید بررسی شود.",
        "usage_or_rate_limit": "پرووایدر به محدودیت نرخ درخواست یا سهمیه رسیده است؛ مراحل ذخیره‌شده قابل استفادهٔ مجدد هستند.",
        "incomplete_response": "پرووایدر پاسخ ساختاریافتهٔ کامل تحویل نداد یا مدل از پاسخ خودداری کرد؛ پاسخ ناقص پذیرفته نشد.",
        "internal_error": "اجرای نمونه به دلیل خطای داخلی متوقف شد؛ شناسهٔ اجرا را برای بررسی ارسال کن.",
    }
    if code == "review_budget_exceeded":
        if details.get("reason") == "cited_evidence_bundle":
            return prefix + "شواهد لازم برای بازبینی از سقف متن یا تصویر تنظیم‌شده در برنامه بیشتر است؛ شواهد حذف نشده و درخواست به مدل ارسال نشده است."
        return (prefix + f"بازبینی بخش {details['section_id']} با ورودی {details['input_tokens_estimate']:,} توکن "
                f"در سقف {details['request_token_budget']:,} توکن جا نشد. مراحل تکمیل‌شده ذخیره‌اند؛ "
                "این محدودیت برنامه است و درخواست به مدل ارسال نشده است.")
    if code in {"gateway_error", "transport_error", "process_timeout", "routing_timeout", "provider_error"}:
        return prefix + "درخواست مدل یا اتصال پرووایدر ناموفق بود. مراحل تکمیل‌شده ذخیره‌اند؛ بعد از رفع مشکل می‌توان اجرا را ادامه داد."
    return prefix + messages.get(code, messages["internal_error"])
