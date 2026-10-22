import argparse
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import config
from app.db import SessionLocal
from app.email_service import send_email
from app.models import (
    CampaignAttachment,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    DeliveryStatus,
    EmailJob,
    EmailJobStatus,
    SuppressedEmail,
)
from app.settings_service import get_settings

logger = logging.getLogger("app")

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


class ThrottleController:
    def __init__(
        self,
        throttle_seconds: float,
        max_per_minute: int,
        max_per_domain_per_minute: int,
    ) -> None:
        self.global_interval = 0.0
        if max_per_minute > 0:
            self.global_interval = 60.0 / max_per_minute
        self.domain_interval = 0.0
        if max_per_domain_per_minute > 0:
            self.domain_interval = 60.0 / max_per_domain_per_minute
        if throttle_seconds > self.global_interval:
            self.global_interval = throttle_seconds
        self._next_global_time = 0.0
        self._next_domain_time: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str) -> None:
        sleep_for = 0.0
        with self._lock:
            now = time.monotonic()
            next_global = self._next_global_time
            next_domain = self._next_domain_time.get(domain, 0.0)
            target = max(now, next_global, next_domain)
            if self.global_interval:
                self._next_global_time = target + self.global_interval
            if self.domain_interval:
                self._next_domain_time[domain] = target + self.domain_interval
            if target > now:
                sleep_for = target - now
        if sleep_for > 0:
            time.sleep(sleep_for)


def _supports_skip_locked() -> bool:
    return config.DB_URL.startswith("postgresql")


def _recover_stuck_jobs(db: Session, sending_timeout_seconds: int) -> int:
    timeout_seconds = max(int(sending_timeout_seconds), 30)
    now = datetime.now()
    cutoff = now - timedelta(seconds=timeout_seconds)
    sending_jobs = (
        db.query(EmailJob)
        .filter(
            EmailJob.status == EmailJobStatus.SENDING,
        )
        .all()
    )
    stuck_jobs = [
        job
        for job in sending_jobs
        if (job.last_attempt_at and job.last_attempt_at < cutoff)
        or (job.last_attempt_at is None and job.updated_at and job.updated_at < cutoff)
    ]
    if not stuck_jobs:
        return 0
    recovered = 0
    marker = f"Recovered by watchdog at {now.isoformat(timespec='seconds')}"
    for job in stuck_jobs:
        job.status = EmailJobStatus.QUEUED
        job.next_attempt_at = now
        job.last_error = marker
        recovered += 1
    logger.warning("Email watchdog recovered %s stuck jobs", recovered)
    return recovered


def _get_jobs(db: Session, limit: int) -> list[EmailJob]:
    now = datetime.now()
    query = (
        db.query(EmailJob)
        .filter(
            EmailJob.status == EmailJobStatus.QUEUED,
            EmailJob.next_attempt_at <= now,
        )
        .order_by(EmailJob.priority.desc(), EmailJob.created_at.asc())
    )
    if _supports_skip_locked():
        query = query.with_for_update(skip_locked=True)
    return query.limit(limit).all()


def _is_suppressed(db: Session, tenant_id: int, email: str) -> bool:
    return (
        db.query(SuppressedEmail)
        .filter(
            SuppressedEmail.tenant_id == tenant_id,
            SuppressedEmail.email == email,
        )
        .first()
        is not None
    )


def _update_campaign_status(db: Session, campaign_id: int, tenant_id: int) -> None:
    pending = (
        db.query(CampaignRecipient)
        .filter(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.tenant_id == tenant_id,
            CampaignRecipient.delivery_status == DeliveryStatus.PENDING,
        )
        .count()
    )
    if pending:
        return
    campaign = (
        db.query(Campaign)
        .filter(Campaign.id == campaign_id, Campaign.tenant_id == tenant_id)
        .first()
    )
    if not campaign:
        return
    campaign.status = CampaignStatus.SENT
    if not campaign.sent_at:
        campaign.sent_at = datetime.now()


def _get_throttle(
    cache: dict[int, ThrottleController],
    cache_lock: threading.Lock,
    tenant_id: int,
    settings,
) -> ThrottleController:
    signature = (
        settings.throttle_seconds,
        settings.max_emails_per_minute,
        settings.max_emails_per_domain_per_minute,
    )
    with cache_lock:
        existing = cache.get(tenant_id)
        if existing and getattr(existing, "_signature", None) == signature:
            return existing
        throttle = ThrottleController(*signature)
        throttle._signature = signature
        cache[tenant_id] = throttle
        return throttle


def _process_job(
    db: Session,
    job: EmailJob,
    settings,
    throttle: ThrottleController,
) -> None:

    job.attempts += 1
    recipient = (
        db.query(CampaignRecipient)
        .filter(
            CampaignRecipient.id == job.recipient_id,
            CampaignRecipient.tenant_id == job.tenant_id,
        )
        .first()
    )
    if not recipient:
        job.status = EmailJobStatus.FAILED
        job.last_error = "Missing recipient"
        return

    email = job.to_email.strip().lower()
    if not email or _is_suppressed(db, job.tenant_id, email):
        job.status = EmailJobStatus.FAILED
        job.last_error = "Suppressed email"
        recipient.delivery_status = DeliveryStatus.FAILED
        recipient.last_error = job.last_error
        _update_campaign_status(db, job.campaign_id, job.tenant_id)
        return

    domain = email.split("@")[-1] if "@" in email else "unknown"
    throttle.wait(domain)

    try:
        attachment_rows = (
            db.query(CampaignAttachment)
            .filter(
                CampaignAttachment.tenant_id == job.tenant_id,
                CampaignAttachment.campaign_id == job.campaign_id,
            )
            .order_by(CampaignAttachment.created_at.asc())
            .all()
        )
        attachments = [
            {
                "file_name": row.file_name,
                "file_path": row.file_path,
            }
            for row in attachment_rows
        ]
        send_email(job.subject, job.to_email, job.html_body, settings, attachments=attachments)
        job.status = EmailJobStatus.SENT
        job.sent_at = datetime.now()
        job.last_error = None
        recipient.delivery_status = DeliveryStatus.SENT
        recipient.sent_at = datetime.now()
        recipient.last_error = None
        _update_campaign_status(db, job.campaign_id, job.tenant_id)
    except Exception as exc:
        job.last_error = str(exc)
        job.last_attempt_at = datetime.now()
        if job.attempts >= job.max_attempts:
            job.status = EmailJobStatus.FAILED
            recipient.delivery_status = DeliveryStatus.FAILED
            recipient.last_error = job.last_error
            _update_campaign_status(db, job.campaign_id, job.tenant_id)
        else:
            job.status = EmailJobStatus.QUEUED
            delay = max(settings.retry_base_delay_seconds, 1) * (2 ** (job.attempts - 1))
            job.next_attempt_at = datetime.now() + timedelta(seconds=delay)

def _get_settings_cached(
    db: Session,
    settings_cache: dict[int, object],
    settings_lock: threading.Lock,
    tenant_id: int,
):
    # Always reload settings so SMTP updates take effect without restarting worker.
    # Cache is intentionally bypassed.
    return get_settings(db, tenant_id)


def _process_job_id(
    job_id: int,
    throttle_by_tenant: dict[int, ThrottleController],
    throttle_lock: threading.Lock,
    settings_cache: dict[int, object],
    settings_lock: threading.Lock,
) -> None:
    db = SessionLocal()
    try:
        job = db.query(EmailJob).filter(EmailJob.id == job_id).first()
        if not job or job.status != EmailJobStatus.SENDING:
            return
        settings = _get_settings_cached(db, settings_cache, settings_lock, job.tenant_id)
        throttle = _get_throttle(throttle_by_tenant, throttle_lock, job.tenant_id, settings)
        _process_job(db, job, settings, throttle)
        db.commit()
    finally:
        db.close()


def run_worker(
    batch_size: int,
    poll_interval: float,
    once: bool,
    parallel: int,
    stuck_timeout_seconds: int,
) -> None:
    logger.info("Email worker started")
    throttle_by_tenant: dict[int, ThrottleController] = {}
    throttle_lock = threading.Lock()
    settings_cache: dict[int, object] = {}
    settings_lock = threading.Lock()
    parallel = max(1, parallel)
    batch_size = max(batch_size, parallel)
    stuck_timeout_seconds = max(30, stuck_timeout_seconds)
    executor = ThreadPoolExecutor(max_workers=parallel)
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

            job_ids = [job.id for job in jobs]
            for job in jobs:
                job.status = EmailJobStatus.SENDING
                job.last_attempt_at = datetime.now()
            db.commit()
        finally:
            db.close()

        futures = [
            executor.submit(
                _process_job_id,
                job_id,
                throttle_by_tenant,
                throttle_lock,
                settings_cache,
                settings_lock,
            )
            for job_id in job_ids
        ]
        for future in futures:
            future.result()

        if once:
            executor.shutdown(wait=True)
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Email queue worker.")
    parser.add_argument("--batch", type=int, default=10, help="Number of jobs to process per batch.")
    parser.add_argument("--poll", type=float, default=2.0, help="Poll interval in seconds.")
    parser.add_argument("--parallel", type=int, default=3, help="Number of parallel sends.")
    parser.add_argument(
        "--stuck-timeout",
        type=int,
        default=600,
        help="Seconds before a SENDING job is considered stuck and re-queued.",
    )
    parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
    args = parser.parse_args()

    run_worker(args.batch, args.poll, args.once, args.parallel, args.stuck_timeout)


if __name__ == "__main__":
    main()
