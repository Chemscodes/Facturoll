from __future__ import annotations

import json
import random
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from app import config
from app.services.openai_observability import record_openai_api_call


def extract_chat_message_content(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        content = "\n".join(parts)
    return content if isinstance(content, str) else ""


def call_chat_completion_json_with_retry(
    *,
    operation: str,
    payload: dict[str, Any],
    primary_model: str,
    fallback_model: str | None = None,
    timeout_s: int = 60,
    retry_attempts: int = 3,
    retry_backoff_s: float = 1.5,
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not config.OPENAI_API_KEY:
        return None, None

    model_candidates: list[str] = []
    primary = str(primary_model or "").strip()
    fallback = str(fallback_model or "").strip()
    if primary:
        model_candidates.append(primary)
    if fallback and fallback != primary:
        model_candidates.append(fallback)
    if not model_candidates:
        model_candidates = ["gpt-5.1"]

    timeout_s = max(10, int(timeout_s or 0))
    retry_attempts = max(1, int(retry_attempts or 0))
    retry_backoff_s = max(0.1, float(retry_backoff_s or 0.0))
    headers = {
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    metadata = dict(metadata or {})

    for model in model_candidates:
        model_payload = dict(payload)
        model_payload["model"] = model
        for attempt in range(retry_attempts):
            started = time.perf_counter()
            try:
                req = urllib_request.Request(
                    "https://api.openai.com/v1/chat/completions",
                    data=json.dumps(model_payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib_request.urlopen(req, timeout=timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                record_openai_api_call(
                    operation=operation,
                    endpoint="/v1/chat/completions",
                    model=model,
                    response_json=data if isinstance(data, dict) else None,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=True,
                    metadata={
                        **metadata,
                        "attempt": attempt + 1,
                        "using_fallback_model": bool(model != primary),
                    },
                )
            except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, ValueError) as exc:
                status_code = (
                    getattr(exc, "code", None)
                    if isinstance(exc, urllib_error.HTTPError)
                    else None
                )
                retryable = (
                    status_code in {408, 409, 429, 500, 502, 503, 504}
                    or isinstance(exc, (urllib_error.URLError, TimeoutError))
                )
                record_openai_api_call(
                    operation=operation,
                    endpoint="/v1/chat/completions",
                    model=model,
                    response_json=None,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error_type=type(exc).__name__,
                    metadata={
                        **metadata,
                        "attempt": attempt + 1,
                        "retryable": bool(retryable),
                        "http_status": status_code,
                        "using_fallback_model": bool(model != primary),
                    },
                )
                if retryable and attempt < (retry_attempts - 1):
                    delay = (retry_backoff_s * (2**attempt)) + random.uniform(0.0, 0.35)
                    time.sleep(delay)
                    continue
                break

            try:
                content = extract_chat_message_content(data if isinstance(data, dict) else None)
                if not content.strip():
                    raise ValueError("missing_content")
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("json_not_object")
                return parsed, model
            except Exception as parse_exc:
                record_openai_api_call(
                    operation=f"{operation}_parse",
                    endpoint="/v1/chat/completions",
                    model=model,
                    response_json=data if isinstance(data, dict) else None,
                    latency_ms=0,
                    success=False,
                    error_type=type(parse_exc).__name__,
                    metadata={
                        **metadata,
                        "attempt": attempt + 1,
                        "using_fallback_model": bool(model != primary),
                    },
                )
                if attempt < (retry_attempts - 1):
                    delay = (retry_backoff_s * (2**attempt)) + random.uniform(0.0, 0.35)
                    time.sleep(delay)
                    continue
                break

    return None, None

