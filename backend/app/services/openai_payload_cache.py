from __future__ import annotations

import logging
from typing import Any

from app.db import SessionLocal
from app.models import OpenAIPayloadCache
from app.services.openai_observability import log_openai_cache_event

logger = logging.getLogger("app")


def get_openai_payload_cache(
    *,
    cache_type: str,
    model: str,
    cache_key: str,
) -> Any | None:
    if not cache_type or not model or not cache_key:
        return None
    db = SessionLocal()
    try:
        row = (
            db.query(OpenAIPayloadCache)
            .filter(
                OpenAIPayloadCache.cache_type == cache_type,
                OpenAIPayloadCache.model == model,
                OpenAIPayloadCache.cache_key == cache_key,
            )
            .first()
        )
        if row is None:
            log_openai_cache_event(
                cache_type=cache_type,
                model=model,
                cache_key=cache_key,
                hit=False,
            )
            return None
        log_openai_cache_event(
            cache_type=cache_type,
            model=model,
            cache_key=cache_key,
            hit=True,
        )
        return row.payload
    except Exception as exc:
        logger.warning(
            "OpenAI payload cache read failed: type=%s model=%s key=%s err=%s",
            cache_type,
            model,
            cache_key,
            exc,
        )
        return None
    finally:
        db.close()


def set_openai_payload_cache(
    *,
    cache_type: str,
    model: str,
    cache_key: str,
    payload: Any,
) -> None:
    if not cache_type or not model or not cache_key or payload is None:
        return
    db = SessionLocal()
    try:
        row = (
            db.query(OpenAIPayloadCache)
            .filter(
                OpenAIPayloadCache.cache_type == cache_type,
                OpenAIPayloadCache.model == model,
                OpenAIPayloadCache.cache_key == cache_key,
            )
            .first()
        )
        if row is None:
            row = OpenAIPayloadCache(
                cache_type=cache_type,
                model=model,
                cache_key=cache_key,
                payload=payload,
            )
            db.add(row)
        else:
            row.payload = payload
        db.commit()
        logger.info(
            "OpenAI cache write: type=%s model=%s key=%s",
            cache_type,
            model,
            str(cache_key or "")[:12],
        )
    except Exception as exc:
        db.rollback()
        logger.warning(
            "OpenAI payload cache write failed: type=%s model=%s key=%s err=%s",
            cache_type,
            model,
            cache_key,
            exc,
        )
    finally:
        db.close()
