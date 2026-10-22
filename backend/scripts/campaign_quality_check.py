from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import func

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.models import (
    Campaign,
    CampaignAttachment,
    CampaignItem,
    InboundInvoice,
    InvoiceExtractionItem,
    OfferAnalysisResult,
    TechSheet,
    TechSheetLink,
)


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _campaign_ids(db, campaign_id: int | None, limit: int) -> list[int]:
    if campaign_id is not None:
        return [campaign_id]
    rows = (
        db.query(Campaign.id)
        .order_by(Campaign.id.desc())
        .limit(limit)
        .all()
    )
    return [int(r[0]) for r in rows]


def _run_for_campaign(db, campaign_id: int) -> dict[str, object]:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return {"campaign_id": campaign_id, "error": "not_found"}

    tenant_id = campaign.tenant_id

    invoice_items_count = (
        db.query(func.count(InvoiceExtractionItem.id))
        .filter(
            InvoiceExtractionItem.campaign_id == campaign_id,
            InvoiceExtractionItem.tenant_id == tenant_id,
        )
        .scalar()
        or 0
    )
    missing_qty_count = (
        db.query(func.count(InvoiceExtractionItem.id))
        .filter(
            InvoiceExtractionItem.campaign_id == campaign_id,
            InvoiceExtractionItem.tenant_id == tenant_id,
            func.coalesce(
                InvoiceExtractionItem.corrected_quantity,
                InvoiceExtractionItem.quantity,
            ).is_(None),
        )
        .scalar()
        or 0
    )
    unknown_name_count = (
        db.query(func.count(InvoiceExtractionItem.id))
        .filter(
            InvoiceExtractionItem.campaign_id == campaign_id,
            InvoiceExtractionItem.tenant_id == tenant_id,
            func.coalesce(InvoiceExtractionItem.product_name, "").ilike("unknown"),
        )
        .scalar()
        or 0
    )

    requested_products_count = (
        db.query(func.count(func.distinct(CampaignItem.product_id)))
        .filter(
            CampaignItem.campaign_id == campaign_id,
            CampaignItem.tenant_id == tenant_id,
        )
        .scalar()
        or 0
    )

    offers_count = (
        db.query(func.count(OfferAnalysisResult.id))
        .filter(
            OfferAnalysisResult.campaign_id == campaign_id,
            OfferAnalysisResult.tenant_id == tenant_id,
        )
        .scalar()
        or 0
    )
    offers_score_zero = (
        db.query(func.count(OfferAnalysisResult.id))
        .filter(
            OfferAnalysisResult.campaign_id == campaign_id,
            OfferAnalysisResult.tenant_id == tenant_id,
            func.coalesce(OfferAnalysisResult.tech_sheet_similarity_display_score, 0.0)
            <= 0.0,
        )
        .scalar()
        or 0
    )
    offers_no_tech = (
        db.query(func.count(OfferAnalysisResult.id))
        .filter(
            OfferAnalysisResult.campaign_id == campaign_id,
            OfferAnalysisResult.tenant_id == tenant_id,
            OfferAnalysisResult.tech_sheet_id.is_(None),
        )
        .scalar()
        or 0
    )

    requested_products_without_offers = (
        db.query(func.count(func.distinct(CampaignItem.product_id)))
        .outerjoin(
            OfferAnalysisResult,
            OfferAnalysisResult.requested_product_id == CampaignItem.product_id,
        )
        .filter(
            CampaignItem.campaign_id == campaign_id,
            CampaignItem.tenant_id == tenant_id,
            OfferAnalysisResult.id.is_(None),
        )
        .scalar()
        or 0
    )

    invoices_count = (
        db.query(func.count(InboundInvoice.id))
        .filter(
            InboundInvoice.campaign_id == campaign_id,
            InboundInvoice.tenant_id == tenant_id,
        )
        .scalar()
        or 0
    )
    attachments_count = (
        db.query(func.count(CampaignAttachment.id))
        .filter(
            CampaignAttachment.campaign_id == campaign_id,
            CampaignAttachment.tenant_id == tenant_id,
        )
        .scalar()
        or 0
    )
    tech_sheets_count = (
        db.query(func.count(TechSheet.id))
        .filter(
            TechSheet.campaign_id == campaign_id,
            TechSheet.tenant_id == tenant_id,
        )
        .scalar()
        or 0
    )
    tech_links_count = (
        db.query(func.count(TechSheetLink.id))
        .filter(
            TechSheetLink.campaign_id == campaign_id,
            TechSheetLink.tenant_id == tenant_id,
        )
        .scalar()
        or 0
    )

    def ratio(part: int, total: int) -> float | None:
        if total <= 0:
            return None
        return round(part / total, 4)

    return {
        "campaign_id": campaign_id,
        "title": campaign.title,
        "status": campaign.status.value if campaign.status else None,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "invoice_items_count": int(invoice_items_count),
        "missing_qty_count": int(missing_qty_count),
        "missing_qty_ratio": ratio(int(missing_qty_count), int(invoice_items_count)),
        "unknown_name_count": int(unknown_name_count),
        "unknown_name_ratio": ratio(int(unknown_name_count), int(invoice_items_count)),
        "requested_products_count": int(requested_products_count),
        "requested_products_without_offers": int(requested_products_without_offers),
        "offers_count": int(offers_count),
        "offers_score_zero": int(offers_score_zero),
        "offers_score_zero_ratio": ratio(int(offers_score_zero), int(offers_count)),
        "offers_no_tech_sheet": int(offers_no_tech),
        "tech_sheets_count": int(tech_sheets_count),
        "tech_links_count": int(tech_links_count),
        "invoices_count": int(invoices_count),
        "attachments_count": int(attachments_count),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Campaign quality check (read-only).")
    parser.add_argument("--campaign-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--output-json",
        default=str(Path("reports") / "campaign_checks_latest.json"),
    )
    parser.add_argument(
        "--output-csv",
        default=str(Path("reports") / "campaign_checks_latest.csv"),
    )
    args = parser.parse_args()

    out_rows: list[dict[str, object]] = []
    with SessionLocal() as db:
        for cid in _campaign_ids(db, args.campaign_id, args.limit):
            out_rows.append(_run_for_campaign(db, cid))

    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(out_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    headers = sorted({k for row in out_rows for k in row.keys()})
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in out_rows:
            writer.writerow(row)

    print(f"Saved JSON: {output_json}")
    print(f"Saved CSV: {output_csv}")
    print(f"Rows: {len(out_rows)} | Generated at: {datetime.utcnow().isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
