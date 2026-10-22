from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import config
from app.models import (
    Campaign,
    CampaignAttachment,
    CampaignItem,
    CampaignResultsCache,
    InboundInvoice,
    InvoiceExtractionItem,
    OfferAnalysisResult,
    ExtractionStatus,
    Response as ResponseModel,
    ResponseItem,
    TechSheet,
    TechSheetLink,
)


RESULTS_CACHE_SCHEMA_VERSION = 10
RESULTS_CACHE_KEY = "results_v1"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_results_cache_signature(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    campaign_id: int,
    confirm_threshold: float,
    verify_threshold: float,
    characteristics_threshold: float,
    offer_limit: int | None,
    conform_only: bool,
    useful_only: bool,
    min_match_score: float,
) -> dict[str, Any]:
    campaign_mode = (
        db.query(Campaign.processing_mode)
        .filter(
            Campaign.tenant_id == tenant_id,
            Campaign.id == campaign_id,
        )
        .scalar()
    )
    (
        attachments_count,
        attachments_max_id,
        attachments_latest_created,
        attachments_latest_extracted,
        attachments_with_preview,
        attachments_with_strict_preview,
    ) = (
        db.query(
            func.count(CampaignAttachment.id),
            func.max(CampaignAttachment.id),
            func.max(CampaignAttachment.created_at),
            func.max(CampaignAttachment.extracted_at),
            func.count(CampaignAttachment.id).filter(
                CampaignAttachment.extraction_preview.isnot(None)
            ),
            func.count(CampaignAttachment.id).filter(
                CampaignAttachment.strict_extraction_preview.isnot(None)
            ),
        )
        .filter(
            CampaignAttachment.tenant_id == tenant_id,
            CampaignAttachment.campaign_id == campaign_id,
        )
        .one()
    )
    campaign_items_count, campaign_items_max_id = (
        db.query(
            func.count(CampaignItem.id),
            func.max(CampaignItem.id),
        )
        .filter(
            CampaignItem.tenant_id == tenant_id,
            CampaignItem.campaign_id == campaign_id,
        )
        .one()
    )
    response_items_count, response_items_max_id, responses_latest = (
        db.query(
            func.count(ResponseItem.id),
            func.max(ResponseItem.id),
            func.max(ResponseModel.submitted_at),
        )
        .join(ResponseModel, ResponseItem.response_id == ResponseModel.id)
        .filter(
            ResponseItem.tenant_id == tenant_id,
            ResponseModel.tenant_id == tenant_id,
            ResponseModel.campaign_id == campaign_id,
        )
        .one()
    )
    invoice_items_count, invoice_items_max_id, invoice_items_latest = (
        db.query(
            func.count(InvoiceExtractionItem.id),
            func.max(InvoiceExtractionItem.id),
            func.max(InvoiceExtractionItem.updated_at),
        )
        .filter(
            InvoiceExtractionItem.tenant_id == tenant_id,
            InvoiceExtractionItem.campaign_id == campaign_id,
        )
        .one()
    )
    invoice_items_confirmed = (
        db.query(func.count(InvoiceExtractionItem.id))
        .filter(
            InvoiceExtractionItem.tenant_id == tenant_id,
            InvoiceExtractionItem.campaign_id == campaign_id,
            InvoiceExtractionItem.status == ExtractionStatus.CONFIRMED,
        )
        .scalar()
    )
    invoice_items_to_verify = (
        db.query(func.count(InvoiceExtractionItem.id))
        .filter(
            InvoiceExtractionItem.tenant_id == tenant_id,
            InvoiceExtractionItem.campaign_id == campaign_id,
            InvoiceExtractionItem.status == ExtractionStatus.TO_VERIFY,
        )
        .scalar()
    )
    invoice_items_pending = (
        db.query(func.count(InvoiceExtractionItem.id))
        .filter(
            InvoiceExtractionItem.tenant_id == tenant_id,
            InvoiceExtractionItem.campaign_id == campaign_id,
            InvoiceExtractionItem.status == ExtractionStatus.PENDING,
        )
        .scalar()
    )
    invoices_count, invoices_latest = (
        db.query(
            func.count(InboundInvoice.id),
            func.max(InboundInvoice.received_at),
        )
        .filter(
            InboundInvoice.tenant_id == tenant_id,
            InboundInvoice.campaign_id == campaign_id,
        )
        .one()
    )
    tech_sheets_count, tech_sheets_max_id, tech_sheets_latest = (
        db.query(
            func.count(TechSheet.id),
            func.max(TechSheet.id),
            func.max(TechSheet.created_at),
        )
        .filter(
            TechSheet.tenant_id == tenant_id,
            TechSheet.campaign_id == campaign_id,
        )
        .one()
    )
    tech_links_count, tech_links_max_id, tech_links_latest = (
        db.query(
            func.count(TechSheetLink.id),
            func.max(TechSheetLink.id),
            func.max(TechSheetLink.created_at),
        )
        .filter(
            TechSheetLink.tenant_id == tenant_id,
            TechSheetLink.campaign_id == campaign_id,
        )
        .one()
    )
    offer_analysis_count, offer_analysis_max_id, offer_analysis_latest = (
        db.query(
            func.count(OfferAnalysisResult.id),
            func.max(OfferAnalysisResult.id),
            func.max(OfferAnalysisResult.updated_at),
        )
        .filter(
            OfferAnalysisResult.tenant_id == tenant_id,
            OfferAnalysisResult.campaign_id == campaign_id,
        )
        .one()
    )

    return {
        "schema_version": RESULTS_CACHE_SCHEMA_VERSION,
        "cache_key": RESULTS_CACHE_KEY,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "campaign_id": campaign_id,
        "campaign_processing_mode": str(campaign_mode or "").strip().lower(),
        "offer_limit": offer_limit,
        "conform_only": bool(conform_only),
        "useful_only": bool(useful_only),
        "results_filter_mode": "gpt_match_rate",
        "min_match_score": round(float(min_match_score), 4),
        "confirm_threshold": round(float(confirm_threshold), 4),
        "verify_threshold": round(float(verify_threshold), 4),
        "characteristics_threshold": round(float(characteristics_threshold), 4),
        "product_match_mode": str(getattr(config, "OPENAI_PRODUCT_MATCH_MODE", "embedding") or "embedding"),
        "product_match_model": str(getattr(config, "OPENAI_PRODUCT_MATCH_MODEL", "") or ""),
        "tech_similarity_mode": str(getattr(config, "OPENAI_TECH_SIMILARITY_MODE", "local") or "local"),
        "tech_similarity_model": str(getattr(config, "OPENAI_TECH_SIMILARITY_MODEL", "") or ""),
        "invoice_components_mode": str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODE", "local") or "local"),
        "campaign_items_count": int(campaign_items_count or 0),
        "campaign_items_max_id": int(campaign_items_max_id or 0),
        "attachments_count": int(attachments_count or 0),
        "attachments_max_id": int(attachments_max_id or 0),
        "attachments_latest_created": _iso(attachments_latest_created),
        "attachments_latest_extracted": _iso(attachments_latest_extracted),
        "attachments_with_preview": int(attachments_with_preview or 0),
        "attachments_with_strict_preview": int(attachments_with_strict_preview or 0),
        "response_items_count": int(response_items_count or 0),
        "response_items_max_id": int(response_items_max_id or 0),
        "responses_latest": _iso(responses_latest),
        "invoice_items_count": int(invoice_items_count or 0),
        "invoice_items_max_id": int(invoice_items_max_id or 0),
        "invoice_items_latest": _iso(invoice_items_latest),
        "invoice_items_confirmed": int(invoice_items_confirmed or 0),
        "invoice_items_to_verify": int(invoice_items_to_verify or 0),
        "invoice_items_pending": int(invoice_items_pending or 0),
        "invoices_count": int(invoices_count or 0),
        "invoices_latest": _iso(invoices_latest),
        "tech_sheets_count": int(tech_sheets_count or 0),
        "tech_sheets_max_id": int(tech_sheets_max_id or 0),
        "tech_sheets_latest": _iso(tech_sheets_latest),
        "tech_links_count": int(tech_links_count or 0),
        "tech_links_max_id": int(tech_links_max_id or 0),
        "tech_links_latest": _iso(tech_links_latest),
        "offer_analysis_count": int(offer_analysis_count or 0),
        "offer_analysis_max_id": int(offer_analysis_max_id or 0),
        "offer_analysis_latest": _iso(offer_analysis_latest),
    }


def get_results_cache(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    campaign_id: int,
    signature: dict[str, Any],
    allow_any_user: bool = False,
) -> list[dict[str, Any]] | None:
    def _normalize_signature(raw: Any, *, ignore_user: bool) -> Any:
        if not isinstance(raw, dict):
            return raw
        out = dict(raw)
        if ignore_user:
            out.pop("user_id", None)
        return out

    def _payload_from_row(row: CampaignResultsCache | None) -> list[dict[str, Any]] | None:
        if row is None:
            return None
        if not isinstance(row.payload, list):
            return None
        out: list[dict[str, Any]] = []
        for raw in row.payload:
            if isinstance(raw, dict):
                out.append(dict(raw))
        return out

    row = (
        db.query(CampaignResultsCache)
        .filter(
            CampaignResultsCache.tenant_id == tenant_id,
            CampaignResultsCache.user_id == user_id,
            CampaignResultsCache.campaign_id == campaign_id,
            CampaignResultsCache.cache_key == RESULTS_CACHE_KEY,
        )
        .first()
    )
    if (
        row is not None
        and isinstance(row.signature, dict)
        and row.signature == signature
    ):
        return _payload_from_row(row)

    if not allow_any_user:
        return None

    expected = _normalize_signature(signature, ignore_user=True)
    rows = (
        db.query(CampaignResultsCache)
        .filter(
            CampaignResultsCache.tenant_id == tenant_id,
            CampaignResultsCache.campaign_id == campaign_id,
            CampaignResultsCache.cache_key == RESULTS_CACHE_KEY,
        )
        .order_by(CampaignResultsCache.updated_at.desc())
        .all()
    )
    for candidate in rows:
        if not isinstance(candidate.signature, dict):
            continue
        if _normalize_signature(candidate.signature, ignore_user=True) != expected:
            continue
        return _payload_from_row(candidate)
    return None


def set_results_cache(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    campaign_id: int,
    signature: dict[str, Any],
    payload: list[dict[str, Any]],
) -> None:
    row = (
        db.query(CampaignResultsCache)
        .filter(
            CampaignResultsCache.tenant_id == tenant_id,
            CampaignResultsCache.user_id == user_id,
            CampaignResultsCache.campaign_id == campaign_id,
            CampaignResultsCache.cache_key == RESULTS_CACHE_KEY,
        )
        .first()
    )
    if row is None:
        row = CampaignResultsCache(
            tenant_id=tenant_id,
            user_id=user_id,
            campaign_id=campaign_id,
            cache_key=RESULTS_CACHE_KEY,
        )
    row.signature = signature
    row.payload = payload
    row.built_at = datetime.utcnow()
    db.add(row)
    db.commit()


def invalidate_results_cache(
    db: Session,
    *,
    tenant_id: int,
    campaign_id: int,
    user_id: int | None = None,
) -> None:
    query = db.query(CampaignResultsCache).filter(
        CampaignResultsCache.tenant_id == tenant_id,
        CampaignResultsCache.campaign_id == campaign_id,
        CampaignResultsCache.cache_key == RESULTS_CACHE_KEY,
    )
    if user_id is not None:
        query = query.filter(CampaignResultsCache.user_id == user_id)
    query.delete(synchronize_session=False)
    db.commit()
