from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from typing import Dict, List, Tuple

import httpx
from sqlalchemy.orm import Session

from app import config
from app.db import SessionLocal
from app.models import EmbeddingCache, EmbeddingJob, EmbeddingJobStatus
from app.services.openai_observability import record_openai_api_call

_last_request_ts = 0.0
logger = logging.getLogger("app")


def _throttle(min_interval_seconds: float) -> None:
    global _last_request_ts
    now = time.monotonic()
    wait = (_last_request_ts + min_interval_seconds) - now
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()

def _min_interval() -> float:
    rps = max(config.OPENAI_EMBEDDING_RPS, 0.01)
    return 1.0 / rps


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get_cached_embeddings(db: Session, model: str, hashes: List[str]) -> Dict[str, List[float]]:
    if not hashes:
        return {}
    rows = (
        db.query(EmbeddingCache)
        .filter(EmbeddingCache.model == model, EmbeddingCache.text_hash.in_(hashes))
        .all()
    )
    return {row.text_hash: row.embedding for row in rows}


def get_cached_embeddings(texts: List[str]) -> Tuple[Dict[str, List[float]], List[str], List[str]]:
    model = config.OPENAI_EMBEDDING_MODEL
    normalized = [_normalize_text(t or "") for t in texts]
    hashes = [_hash_text(t) for t in normalized]
    db = SessionLocal()
    try:
        cached = _get_cached_embeddings(db, model, list(set(hashes)))
    finally:
        db.close()

    missing_texts = []
    missing_hashes = []
    for t, h in zip(texts, hashes):
        if h not in cached:
            missing_texts.append(t)
            missing_hashes.append(h)
    logger.info(
        "Embedding cache summary: model=%s requested=%s cached=%s missing=%s",
        model,
        len(texts),
        len(cached),
        len(missing_texts),
    )
    return cached, missing_texts, missing_hashes


def enqueue_embeddings(texts: List[str]) -> None:
    if not texts:
        return
    model = config.OPENAI_EMBEDDING_MODEL
    normalized = [_normalize_text(t or "") for t in texts]
    hashes = [_hash_text(t) for t in normalized]
    db = SessionLocal()
    try:
        existing_cache = _get_cached_embeddings(db, model, list(set(hashes)))
        existing_jobs = (
            db.query(EmbeddingJob)
            .filter(EmbeddingJob.model == model, EmbeddingJob.text_hash.in_(hashes))
            .all()
        )
        existing_job_hashes = {j.text_hash for j in existing_jobs}

        for t, h in zip(normalized, hashes):
            if h in existing_cache or h in existing_job_hashes:
                continue
            db.add(
                EmbeddingJob(
                    model=model,
                    text_hash=h,
                    text=t,
                    status=EmbeddingJobStatus.QUEUED,
                )
            )
        try:
            db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


def fetch_embeddings(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    if not config.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    last_exc: Exception | None = None
    results: List[List[float]] = []
    batch_size = max(1, config.OPENAI_EMBEDDING_BATCH_SIZE)
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            payload = {
                "model": config.OPENAI_EMBEDDING_MODEL,
                "input": batch,
            }
            for attempt in range(6):
                started = time.perf_counter()
                try:
                    # Throttle by configured RPS
                    _throttle(_min_interval())
                    response = client.post(
                        "https://api.openai.com/v1/embeddings",
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                    data = response.json()
                    record_openai_api_call(
                        operation="embeddings_fetch",
                        endpoint="/v1/embeddings",
                        model=str(config.OPENAI_EMBEDDING_MODEL or ""),
                        response_json=data if isinstance(data, dict) else None,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        success=True,
                        metadata={"batch_size": len(batch)},
                    )
                    items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
                    results.extend([item["embedding"] for item in items])
                    break
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                    status = exc.response.status_code
                    response_json = None
                    try:
                        response_json = exc.response.json()
                    except Exception:
                        response_json = None
                    record_openai_api_call(
                        operation="embeddings_fetch",
                        endpoint="/v1/embeddings",
                        model=str(config.OPENAI_EMBEDDING_MODEL or ""),
                        response_json=response_json if isinstance(response_json, dict) else None,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        success=False,
                        error_type=f"http_{status}",
                        metadata={"batch_size": len(batch), "attempt": attempt + 1},
                    )
                    if status == 429 and attempt < 5:
                        backoff = (2 ** attempt) + random.random() + 2
                        time.sleep(backoff)
                        continue
                    raise
            else:
                raise last_exc or RuntimeError("Embedding request failed")

    return results


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
