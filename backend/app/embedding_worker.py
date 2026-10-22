import argparse
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    EmbeddingJob,
    EmbeddingJobStatus,
    EmbeddingCache,
    InvoiceExtractionItem,
    CampaignItem,
    Product,
    ExtractionStatus,
)
from app.services.openai_embeddings import fetch_embeddings
from app.services.invoice_extraction import match_products_with_embeddings

logger = logging.getLogger("app")

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _get_jobs(db: Session, limit: int) -> list[EmbeddingJob]:
    now = datetime.now()
    return (
        db.query(EmbeddingJob)
        .filter(
            EmbeddingJob.status == EmbeddingJobStatus.QUEUED,
            EmbeddingJob.next_attempt_at <= now,
        )
        .order_by(EmbeddingJob.created_at.asc())
        .limit(limit)
        .all()
    )


def _recover_stuck_jobs(db: Session, processing_timeout_seconds: int) -> int:
    timeout_seconds = max(int(processing_timeout_seconds), 60)
    now = datetime.now()
    cutoff = now - timedelta(seconds=timeout_seconds)
    stuck_jobs = (
        db.query(EmbeddingJob)
        .filter(
            EmbeddingJob.status == EmbeddingJobStatus.PROCESSING,
            EmbeddingJob.updated_at < cutoff,
        )
        .all()
    )
    if not stuck_jobs:
        return 0
    recovered = 0
    marker = f"Recovered by watchdog at {now.isoformat(timespec='seconds')}"
    for job in stuck_jobs:
        job.status = EmbeddingJobStatus.QUEUED
        job.next_attempt_at = now
        job.last_error = marker
        recovered += 1
    logger.warning("Embedding watchdog recovered %s stuck jobs", recovered)
    return recovered


def _store_embeddings(db: Session, jobs: list[EmbeddingJob], vectors: list[list[float]]) -> None:
    for job, vec in zip(jobs, vectors):
        exists = (
            db.query(EmbeddingCache)
            .filter(
                EmbeddingCache.model == job.model,
                EmbeddingCache.text_hash == job.text_hash,
            )
            .first()
        )
        if not exists:
            db.add(
                EmbeddingCache(
                    model=job.model,
                    text_hash=job.text_hash,
                    text=job.text,
                    embedding=vec,
                )
            )
        job.status = EmbeddingJobStatus.DONE
        job.last_error = None


def _recompute_pending_matches(db: Session) -> None:
    pending = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.status == ExtractionStatus.TO_VERIFY,
            InvoiceExtractionItem.notes == "embedding_pending",
        )
        .all()
    )
    if not pending:
        return

    by_campaign: dict[int, list[InvoiceExtractionItem]] = {}
    for item in pending:
        by_campaign.setdefault(item.campaign_id, []).append(item)

    for campaign_id, items in by_campaign.items():
        product_rows = (
            db.query(Product)
            .join(CampaignItem, CampaignItem.product_id == Product.id)
            .filter(
                CampaignItem.campaign_id == campaign_id,
                CampaignItem.tenant_id == items[0].tenant_id,
                Product.tenant_id == items[0].tenant_id,
            )
            .all()
        )
        product_names = [p.name for p in product_rows]
        if not product_names:
            continue
        match_products_with_embeddings(items, product_names)


def run_worker(batch_size: int, poll_interval: float, once: bool, stuck_timeout_seconds: int) -> None:
    logger.info("Embedding worker started")
    batch_size = max(batch_size, 1)
    stuck_timeout_seconds = max(60, stuck_timeout_seconds)
    while True:
        db = SessionLocal()
        try:
            recovered = _recover_stuck_jobs(db, stuck_timeout_seconds)
            if recovered:
                db.commit()
            jobs = _get_jobs(db, batch_size)
            if not jobs:
                db.close()
                if once:
                    return
                time.sleep(poll_interval)
                continue

            for job in jobs:
                job.status = EmbeddingJobStatus.PROCESSING
                job.attempts += 1
            db.commit()

            texts = [j.text for j in jobs]
            try:
                vectors = fetch_embeddings(texts)
                _store_embeddings(db, jobs, vectors)
                _recompute_pending_matches(db)
                db.commit()
            except Exception as exc:
                for job in jobs:
                    job.status = EmbeddingJobStatus.QUEUED
                    job.last_error = str(exc)
                    delay = max(2, 2 ** min(job.attempts, 6))
                    job.next_attempt_at = datetime.now() + timedelta(seconds=delay)
                db.commit()
        finally:
            db.close()

        if once:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Embedding queue worker.")
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--poll", type=float, default=3.0)
    parser.add_argument(
        "--stuck-timeout",
        type=int,
        default=900,
        help="Seconds before a PROCESSING job is considered stuck and re-queued.",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_worker(args.batch, args.poll, args.once, args.stuck_timeout)


if __name__ == "__main__":
    main()
