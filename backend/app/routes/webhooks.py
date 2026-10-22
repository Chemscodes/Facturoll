import json
import logging
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import config
from app.db import get_db
from app.models import SuppressedEmail, SuppressionReason, Supplier

logger = logging.getLogger("app")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _require_token(request: Request) -> None:
    if not config.WEBHOOK_TOKEN:
        return
    token = request.headers.get("X-Webhook-Token") or request.query_params.get("token")
    if token != config.WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid webhook token")


def _tenant_ids_for_email(db: Session, email: str) -> list[int]:
    rows = (
        db.query(Supplier.tenant_id)
        .filter(Supplier.email.ilike(email))
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def _suppress_email(
    db: Session,
    tenant_id: int,
    email: str,
    reason: SuppressionReason,
    details: str,
) -> None:
    existing = (
        db.query(SuppressedEmail)
        .filter(SuppressedEmail.tenant_id == tenant_id, SuppressedEmail.email == email)
        .first()
    )
    if existing:
        existing.reason = reason
        existing.details = details
        return
    db.add(
        SuppressedEmail(
            tenant_id=tenant_id,
            email=email,
            reason=reason,
            details=details,
        )
    )


def _normalize_emails(items: Iterable[dict], key: str) -> list[str]:
    emails: list[str] = []
    for entry in items:
        raw = (entry.get(key) or "").strip().lower()
        if raw:
            emails.append(raw)
    return emails


@router.post("/ses")
async def ses_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    _require_token(request)
    payload = await request.json()
    message_type = payload.get("Type")
    if message_type == "SubscriptionConfirmation":
        return {"status": "ok"}

    message = payload.get("Message")
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("SES webhook message is not valid JSON")
            return {"status": "ignored"}
    if not isinstance(message, dict):
        return {"status": "ignored"}

    notification_type = message.get("notificationType")
    if notification_type not in {"Bounce", "Complaint"}:
        return {"status": "ignored"}

    reason = SuppressionReason.BOUNCE if notification_type == "Bounce" else SuppressionReason.COMPLAINT
    recipients: list[str] = []
    if notification_type == "Bounce":
        bounce = message.get("bounce") or {}
        recipients = _normalize_emails(bounce.get("bouncedRecipients", []), "emailAddress")
    if notification_type == "Complaint":
        complaint = message.get("complaint") or {}
        recipients = _normalize_emails(complaint.get("complainedRecipients", []), "emailAddress")

    if not recipients:
        return {"status": "ignored"}

    details = json.dumps(message, ensure_ascii=True)
    updated = 0
    for email in recipients:
        tenant_ids = _tenant_ids_for_email(db, email)
        for tenant_id in tenant_ids:
            _suppress_email(db, tenant_id, email, reason, details)
            updated += 1

    db.commit()
    return {"status": "ok", "updated": updated}
