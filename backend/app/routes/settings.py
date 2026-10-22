from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import (
    Campaign,
    CampaignAttachment,
    GmailAccount,
    InboundInvoice,
    InvoiceExtractionItem,
    TechSheet,
    User,
)
from app.email_service import send_email
from app.schemas import (
    Settings,
    SettingsTestEmail,
    EmbeddingThresholds,
    GmailAccountOut,
    GmailConnectUrlOut,
)
from app.services.gmail_oauth import (
    build_authorization_url,
    choose_redirect_uri,
    create_oauth_state,
)
from app.settings_service import (
    get_settings,
    update_settings,
    get_user_embedding_thresholds,
    update_user_embedding_thresholds,
)

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=Settings)
def read_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Settings:
    return get_settings(db, current_user.tenant_id)


@router.put("", response_model=Settings)
def save_settings(
    payload: Settings,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Settings:
    return update_settings(db, payload, current_user.tenant_id)


@router.post("/test-email")
def test_email(
    payload: SettingsTestEmail,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    settings = get_settings(db, current_user.tenant_id)
    html_body = "<p>This is a test email from the local trial app.</p>"
    send_email("Test Email", payload.to_email, html_body, settings)
    return {"status": "ok"}


@router.get("/embedding-thresholds", response_model=EmbeddingThresholds)
def read_embedding_thresholds(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EmbeddingThresholds:
    return get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)


@router.put("/embedding-thresholds", response_model=EmbeddingThresholds)
def save_embedding_thresholds(
    payload: EmbeddingThresholds,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EmbeddingThresholds:
    return update_user_embedding_thresholds(db, current_user.tenant_id, current_user.id, payload)


@router.get("/gmail/account", response_model=GmailAccountOut | None)
def read_gmail_account(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GmailAccountOut | None:
    account = (
        db.query(GmailAccount)
        .filter(
            GmailAccount.tenant_id == current_user.tenant_id,
            GmailAccount.user_id == current_user.id,
        )
        .first()
    )
    return account


@router.post("/gmail/connect-url", response_model=GmailConnectUrlOut)
def gmail_connect_url(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GmailConnectUrlOut:
    redirect_uri = choose_redirect_uri()
    state = create_oauth_state(current_user.id, current_user.tenant_id, redirect_uri=redirect_uri)
    auth_url, returned_state = build_authorization_url(state, redirect_uri=redirect_uri)
    if returned_state != state:
        raise HTTPException(status_code=500, detail="OAuth state mismatch.")
    return GmailConnectUrlOut(auth_url=auth_url, redirect_uri=redirect_uri)


@router.post("/gmail/disconnect")
def gmail_disconnect(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    account = (
        db.query(GmailAccount)
        .filter(
            GmailAccount.tenant_id == current_user.tenant_id,
            GmailAccount.user_id == current_user.id,
        )
        .first()
    )
    if not account:
        return {"status": "not_connected"}

    db.delete(account)
    db.commit()
    return {"status": "disconnected"}


def _pdf_pages(file_path: str, cache: dict[str, int | None]) -> int | None:
    if file_path in cache:
        return cache[file_path]
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        cache[file_path] = None
        return None
    try:
        reader = PdfReader(file_path)
        pages = len(reader.pages)
    except Exception:
        pages = None
    cache[file_path] = pages
    return pages


@router.get("/cost-report")
def cost_report(
    since_days: int = 1,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    # Keep report windows sane.
    days = max(1, min(int(since_days), 90))
    now = datetime.utcnow()
    since = now - timedelta(days=days)
    page_cache: dict[str, int | None] = {}

    campaign_ids = [
        row[0]
        for row in (
            db.query(Campaign.id)
            .filter(
                Campaign.tenant_id == current_user.tenant_id,
                Campaign.owner_user_id == current_user.id,
            )
            .all()
        )
    ]

    if not campaign_ids:
        return {
            "scope": {
                "user_id": current_user.id,
                "email": current_user.email,
                "campaigns_count": 0,
            },
            "window": {
                "since_utc": since.isoformat(timespec="seconds"),
                "until_utc": now.isoformat(timespec="seconds"),
                "since_days": days,
            },
            "ocr": {
                "document_ai_pages_estimated": 0,
                "invoice_pages_estimated": 0,
                "attachment_pages_estimated": 0,
                "tech_sheet_pages_estimated": 0,
                "invoice_documents_count": 0,
                "attachment_documents_count": 0,
                "tech_sheet_documents_count": 0,
                "unknown_page_count_files": 0,
            },
            "reextract": {
                "attachment_rows_reextracted_estimated": 0,
            },
            "embeddings": {
                "embedding_items_count": 0,
                "by_day": [],
                "note": "User-scoped embedding activity proxy from invoice extraction lines.",
            },
            "notes": [
                "No campaigns owned by this user yet.",
            ],
        }

    # 1) OCR usage from campaign attachments (Document AI OCR only).
    attachment_rows = (
        db.query(CampaignAttachment)
        .filter(
            CampaignAttachment.tenant_id == current_user.tenant_id,
            CampaignAttachment.campaign_id.in_(campaign_ids),
            CampaignAttachment.extracted_at.isnot(None),
            CampaignAttachment.extracted_at >= since,
            CampaignAttachment.parse_engine == "document_ai_ocr",
        )
        .all()
    )
    attachment_pages = 0
    attachment_unknown_pages = 0
    for row in attachment_rows:
        pages = _pdf_pages(row.file_path, page_cache)
        if pages is None:
            attachment_unknown_pages += 1
        else:
            attachment_pages += pages

    # 2) OCR usage from tech sheets (Document AI OCR only).
    tech_rows = (
        db.query(TechSheet)
        .filter(
            TechSheet.tenant_id == current_user.tenant_id,
            TechSheet.campaign_id.in_(campaign_ids),
            TechSheet.created_at >= since,
            TechSheet.parse_engine == "document_ai_ocr",
        )
        .all()
    )
    tech_pages = 0
    tech_unknown_pages = 0
    for row in tech_rows:
        pages = _pdf_pages(row.file_path, page_cache)
        if pages is None:
            tech_unknown_pages += 1
        else:
            tech_pages += pages

    # 3) Invoice OCR usage from extraction items created with source_engine=document_ai.
    invoice_ids = [
        row[0]
        for row in (
            db.query(InvoiceExtractionItem.invoice_id)
            .filter(
                InvoiceExtractionItem.tenant_id == current_user.tenant_id,
                InvoiceExtractionItem.campaign_id.in_(campaign_ids),
                InvoiceExtractionItem.created_at >= since,
                InvoiceExtractionItem.source_engine == "document_ai",
            )
            .distinct()
            .all()
        )
    ]
    invoice_rows: list[InboundInvoice] = []
    if invoice_ids:
        invoice_rows = (
            db.query(InboundInvoice)
            .filter(
                InboundInvoice.tenant_id == current_user.tenant_id,
                InboundInvoice.id.in_(invoice_ids),
            )
            .all()
        )
    invoice_pages = 0
    invoice_unknown_pages = 0
    for row in invoice_rows:
        pages = _pdf_pages(row.file_path, page_cache)
        if pages is None:
            invoice_unknown_pages += 1
        else:
            invoice_pages += pages

    # 4) Re-extraction indicators (DB estimate).
    reextract_rows_estimated = (
        db.query(CampaignAttachment.id)
        .filter(
            CampaignAttachment.tenant_id == current_user.tenant_id,
            CampaignAttachment.campaign_id.in_(campaign_ids),
            CampaignAttachment.extracted_at.isnot(None),
            CampaignAttachment.created_at.isnot(None),
            CampaignAttachment.extracted_at > CampaignAttachment.created_at,
            CampaignAttachment.extracted_at >= since,
        )
        .count()
    )

    # 5) Embedding activity proxy from invoice extraction lines.
    embedding_filter = (
        (InvoiceExtractionItem.notes.like("%emb_score=%"))
        | (InvoiceExtractionItem.notes == "embedding_pending")
    )
    embedding_items_count = (
        db.query(InvoiceExtractionItem.id)
        .filter(
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
            InvoiceExtractionItem.campaign_id.in_(campaign_ids),
            InvoiceExtractionItem.created_at >= since,
            embedding_filter,
        )
        .count()
    )
    embedding_daily_rows = (
        db.query(
            func.date(InvoiceExtractionItem.created_at),
            func.count(InvoiceExtractionItem.id),
        )
        .filter(
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
            InvoiceExtractionItem.campaign_id.in_(campaign_ids),
            InvoiceExtractionItem.created_at >= since,
            embedding_filter,
        )
        .group_by(func.date(InvoiceExtractionItem.created_at))
        .all()
    )
    embedding_by_day: dict[str, int] = {}
    for d, c in embedding_daily_rows:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        embedding_by_day[key] = int(c or 0)

    by_day = []
    for i in range(days):
        day = (since + timedelta(days=i)).date().isoformat()
        by_day.append(
            {
                "date": day,
                "embedding_items_count": int(embedding_by_day.get(day, 0)),
            }
        )

    total_ocr_pages = attachment_pages + tech_pages + invoice_pages
    total_unknown_page_files = attachment_unknown_pages + tech_unknown_pages + invoice_unknown_pages

    return {
        "scope": {
            "user_id": current_user.id,
            "email": current_user.email,
            "campaigns_count": len(campaign_ids),
        },
        "window": {
            "since_utc": since.isoformat(timespec="seconds"),
            "until_utc": now.isoformat(timespec="seconds"),
            "since_days": days,
        },
        "ocr": {
            "document_ai_pages_estimated": total_ocr_pages,
            "invoice_pages_estimated": invoice_pages,
            "attachment_pages_estimated": attachment_pages,
            "tech_sheet_pages_estimated": tech_pages,
            "invoice_documents_count": len(invoice_rows),
            "attachment_documents_count": len(attachment_rows),
            "tech_sheet_documents_count": len(tech_rows),
            "unknown_page_count_files": total_unknown_page_files,
        },
        "reextract": {
            "attachment_rows_reextracted_estimated": int(reextract_rows_estimated),
        },
        "embeddings": {
            "embedding_items_count": int(embedding_items_count),
            "by_day": by_day,
            "note": "User-scoped embedding activity proxy from invoice extraction lines.",
        },
        "notes": [
            "OCR pages are estimates from PDF page counts on documents processed with Document AI.",
        ],
    }
