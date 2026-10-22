from __future__ import annotations

import logging
from typing import Any

from app import config
from app.db import SessionLocal
from app.models import OpenAIUsageLog

logger = logging.getLogger("app")


def _bool_env_enabled(value: Any) -> bool:
    return str(value or "1").strip().lower() not in {"0", "false", "no", "off"}


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _extract_usage(data: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    if not isinstance(data, dict):
        return None, None, None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    prompt_tokens = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion_tokens = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total_tokens = _safe_int(usage.get("total_tokens"))
    if total_tokens is None:
        total = 0
        has_any = False
        if prompt_tokens is not None:
            total += prompt_tokens
            has_any = True
        if completion_tokens is not None:
            total += completion_tokens
            has_any = True
        total_tokens = total if has_any else None
    return prompt_tokens, completion_tokens, total_tokens


def _price_for_model(model: str, endpoint: str) -> tuple[float, float]:
    normalized = str(model or "").strip().lower()
    endpoint_norm = str(endpoint or "").strip().lower()
    if normalized.startswith("text-embedding-3-small") or endpoint_norm.endswith("/embeddings"):
        return (
            float(getattr(config, "OPENAI_PRICE_TEXT_EMBEDDING_3_SMALL_INPUT_PER_1M", 0.0) or 0.0),
            0.0,
        )
    if normalized.startswith("text-embedding-3-large"):
        return (
            float(getattr(config, "OPENAI_PRICE_TEXT_EMBEDDING_3_LARGE_INPUT_PER_1M", 0.0) or 0.0),
            0.0,
        )
    if normalized.startswith("gpt-5.4"):
        return (
            float(getattr(config, "OPENAI_PRICE_GPT_54_INPUT_PER_1M", 0.0) or 0.0),
            float(getattr(config, "OPENAI_PRICE_GPT_54_OUTPUT_PER_1M", 0.0) or 0.0),
        )
    if normalized.startswith("gpt-5.1"):
        return (
            float(getattr(config, "OPENAI_PRICE_GPT_51_INPUT_PER_1M", 0.0) or 0.0),
            float(getattr(config, "OPENAI_PRICE_GPT_51_OUTPUT_PER_1M", 0.0) or 0.0),
        )
    if normalized.startswith("gpt-4.1"):
        return (
            float(getattr(config, "OPENAI_PRICE_GPT_41_INPUT_PER_1M", 0.0) or 0.0),
            float(getattr(config, "OPENAI_PRICE_GPT_41_OUTPUT_PER_1M", 0.0) or 0.0),
        )
    return (
        float(getattr(config, "OPENAI_PRICE_DEFAULT_INPUT_PER_1M", 0.0) or 0.0),
        float(getattr(config, "OPENAI_PRICE_DEFAULT_OUTPUT_PER_1M", 0.0) or 0.0),
    )


def _estimate_cost_usd(
    *,
    model: str,
    endpoint: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    input_cost_per_1m, output_cost_per_1m = _price_for_model(model, endpoint)
    if input_cost_per_1m <= 0 and output_cost_per_1m <= 0:
        return None
    total = 0.0
    if prompt_tokens:
        total += (float(prompt_tokens) / 1_000_000.0) * input_cost_per_1m
    if completion_tokens:
        total += (float(completion_tokens) / 1_000_000.0) * output_cost_per_1m
    return round(total, 8)


def record_openai_api_call(
    *,
    operation: str,
    endpoint: str,
    model: str,
    response_json: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    success: bool = True,
    error_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not _bool_env_enabled(getattr(config, "OPENAI_USAGE_LOG_ENABLED", True)):
        return
    prompt_tokens, completion_tokens, total_tokens = _extract_usage(response_json)
    estimated_cost_usd = _estimate_cost_usd(
        model=model,
        endpoint=endpoint,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    db = SessionLocal()
    try:
        db.add(
            OpenAIUsageLog(
                operation=str(operation or "unknown")[:80],
                endpoint=str(endpoint or "unknown")[:120],
                model=str(model or "unknown")[:100],
                success=bool(success),
                error_type=(str(error_type).strip()[:120] if error_type else None),
                latency_ms=_safe_int(latency_ms),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=estimated_cost_usd,
                metadata_json=metadata if isinstance(metadata, dict) else None,
            )
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning(
            "OpenAI usage log write failed: operation=%s endpoint=%s model=%s err=%s",
            operation,
            endpoint,
            model,
            exc,
        )
    finally:
        db.close()


def log_openai_cache_event(
    *,
    cache_type: str,
    model: str,
    cache_key: str,
    hit: bool,
) -> None:
    if not _bool_env_enabled(getattr(config, "OPENAI_USAGE_LOG_ENABLED", True)):
        return
    state = "hit" if hit else "miss"
    logger.info(
        "OpenAI cache %s: type=%s model=%s key=%s",
        state,
        cache_type,
        model,
        str(cache_key or "")[:12],
    )
    db = SessionLocal()
    try:
        db.add(
            OpenAIUsageLog(
                operation=f"cache_{state}"[:80],
                endpoint="/cache",
                model=str(model or "unknown")[:100],
                success=True,
                metadata_json={
                    "cache_type": str(cache_type or "")[:50],
                    "cache_key_prefix": str(cache_key or "")[:12],
                },
            )
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning(
            "OpenAI cache event log write failed: type=%s model=%s key=%s err=%s",
            cache_type,
            model,
            str(cache_key or "")[:12],
            exc,
        )
    finally:
        db.close()
