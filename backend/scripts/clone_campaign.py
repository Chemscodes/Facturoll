from __future__ import annotations

import argparse
import copy
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Campaign,
    CampaignAttachment,
    CampaignItem,
    CampaignRecipient,
    InvoiceExtractionItem,
    InvoiceItemCharacteristicMatch,
    InboundInvoice,
    Response,
    ResponseItem,
    TechSheet,
    TechSheetLink,
    User,
)
from app.routes.campaigns import warm_results_cache_for_campaign  # noqa: E402


def _clone_json(value):
    return copy.deepcopy(value) if value is not None else None


def clone_campaign(
    source_campaign_id: int,
    title: str | None = None,
    warm_cache: bool = False,
) -> dict[str, int | str | bool]:
    db = SessionLocal()
    try:
        source = db.query(Campaign).filter(Campaign.id == source_campaign_id).first()
        if not source:
            raise RuntimeError(f"Source campaign {source_campaign_id} not found")

        now = datetime.now(UTC).replace(tzinfo=None)
        new_title = (title or f"{source.title} CLONE {now.strftime('%Y%m%d_%H%M%S')}").strip()

        clone = Campaign(
            tenant_id=source.tenant_id,
            owner_user_id=source.owner_user_id,
            title=new_title,
            subject=source.subject,
            message_html=source.message_html,
            priority=source.priority,
            status=source.status,
            sent_at=source.sent_at,
        )
        db.add(clone)
        db.flush()

        counts = {
            "attachments": 0,
            "items": 0,
            "recipients": 0,
            "responses": 0,
            "response_items": 0,
            "invoices": 0,
            "invoice_items": 0,
            "tech_sheets": 0,
            "tech_links": 0,
            "char_matches": 0,
        }

        for row in (
            db.query(CampaignAttachment)
            .filter(CampaignAttachment.campaign_id == source_campaign_id)
            .order_by(CampaignAttachment.id.asc())
        ):
            db.add(
                CampaignAttachment(
                    tenant_id=row.tenant_id,
                    campaign_id=clone.id,
                    file_name=row.file_name,
                    file_path=row.file_path,
                    file_size=row.file_size,
                    parse_engine=row.parse_engine,
                    parse_score=row.parse_score,
                    extraction_preview=_clone_json(row.extraction_preview),
                    extracted_at=row.extracted_at,
                    created_at=row.created_at,
                )
            )
            counts["attachments"] += 1

        for row in (
            db.query(CampaignItem)
            .filter(CampaignItem.campaign_id == source_campaign_id)
            .order_by(CampaignItem.id.asc())
        ):
            db.add(
                CampaignItem(
                    tenant_id=row.tenant_id,
                    campaign_id=clone.id,
                    product_id=row.product_id,
                    requested_qty=row.requested_qty,
                    requested_description=row.requested_description,
                    requested_characteristics=_clone_json(row.requested_characteristics),
                    requested_embedding_score=row.requested_embedding_score,
                )
            )
            counts["items"] += 1

        recipient_id_map: dict[int, int] = {}
        for row in (
            db.query(CampaignRecipient)
            .filter(CampaignRecipient.campaign_id == source_campaign_id)
            .order_by(CampaignRecipient.id.asc())
        ):
            token_expiry = row.token_expires_at
            if not token_expiry:
                token_expiry = now + timedelta(days=30)
            clone_row = CampaignRecipient(
                tenant_id=row.tenant_id,
                campaign_id=clone.id,
                supplier_id=row.supplier_id,
                token=secrets.token_hex(32),
                token_expires_at=token_expiry,
                sent_at=row.sent_at,
                delivery_status=row.delivery_status,
                last_error=row.last_error,
            )
            db.add(clone_row)
            db.flush()
            recipient_id_map[row.id] = clone_row.id
            counts["recipients"] += 1

        response_id_map: dict[int, int] = {}
        for row in (
            db.query(Response)
            .filter(Response.campaign_id == source_campaign_id)
            .order_by(Response.id.asc())
        ):
            clone_row = Response(
                tenant_id=row.tenant_id,
                campaign_id=clone.id,
                supplier_id=row.supplier_id,
                submitted_at=row.submitted_at,
                responder_email=row.responder_email,
                ip=row.ip,
            )
            db.add(clone_row)
            db.flush()
            response_id_map[row.id] = clone_row.id
            counts["responses"] += 1

        for row in (
            db.query(ResponseItem)
            .join(Response, Response.id == ResponseItem.response_id)
            .filter(Response.campaign_id == source_campaign_id)
            .order_by(ResponseItem.id.asc())
        ):
            mapped_response_id = response_id_map.get(row.response_id)
            if not mapped_response_id:
                continue
            db.add(
                ResponseItem(
                    tenant_id=row.tenant_id,
                    response_id=mapped_response_id,
                    product_id=row.product_id,
                    offered_qty=row.offered_qty,
                    unit_price=row.unit_price,
                    currency=row.currency,
                    notes=row.notes,
                )
            )
            counts["response_items"] += 1

        invoice_id_map: dict[int, int] = {}
        for row in (
            db.query(InboundInvoice)
            .filter(InboundInvoice.campaign_id == source_campaign_id)
            .order_by(InboundInvoice.id.asc())
        ):
            clone_row = InboundInvoice(
                tenant_id=row.tenant_id,
                campaign_id=clone.id,
                sender_email=row.sender_email,
                sender_name=row.sender_name,
                sender_phone=row.sender_phone,
                parse_engine=row.parse_engine,
                parse_score=row.parse_score,
                subject=row.subject,
                message_id=row.message_id,
                file_name=row.file_name,
                file_path=row.file_path,
                received_at=row.received_at,
                created_at=row.created_at,
            )
            db.add(clone_row)
            db.flush()
            invoice_id_map[row.id] = clone_row.id
            counts["invoices"] += 1

        invoice_item_id_map: dict[int, int] = {}
        for row in (
            db.query(InvoiceExtractionItem)
            .filter(InvoiceExtractionItem.campaign_id == source_campaign_id)
            .order_by(InvoiceExtractionItem.id.asc())
        ):
            mapped_invoice_id = invoice_id_map.get(row.invoice_id)
            if not mapped_invoice_id:
                continue
            clone_row = InvoiceExtractionItem(
                tenant_id=row.tenant_id,
                invoice_id=mapped_invoice_id,
                campaign_id=clone.id,
                supplier_id=row.supplier_id,
                source_engine=row.source_engine,
                status=row.status,
                product_name=row.product_name,
                description=row.description,
                raw_description=row.raw_description,
                product_name_norm=row.product_name_norm,
                unit=row.unit,
                quantity=row.quantity,
                unit_price=row.unit_price,
                currency=row.currency,
                confidence=row.confidence,
                corrected_product_name=row.corrected_product_name,
                corrected_unit=row.corrected_unit,
                corrected_quantity=row.corrected_quantity,
                corrected_unit_price=row.corrected_unit_price,
                corrected_currency=row.corrected_currency,
                similarity_score=row.similarity_score,
                similarity_threshold=row.similarity_threshold,
                similarity_points=_clone_json(row.similarity_points),
                notes=row.notes,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            db.add(clone_row)
            db.flush()
            invoice_item_id_map[row.id] = clone_row.id
            counts["invoice_items"] += 1

        tech_sheet_id_map: dict[int, int] = {}
        for row in (
            db.query(TechSheet)
            .filter(TechSheet.campaign_id == source_campaign_id)
            .order_by(TechSheet.id.asc())
        ):
            clone_row = TechSheet(
                tenant_id=row.tenant_id,
                campaign_id=clone.id,
                supplier_id=row.supplier_id,
                sender_email=row.sender_email,
                file_name=row.file_name,
                file_path=row.file_path,
                file_hash=row.file_hash,
                parse_engine=row.parse_engine,
                parse_score=row.parse_score,
                raw_text=row.raw_text,
                characteristics=_clone_json(row.characteristics),
                created_at=row.created_at,
            )
            db.add(clone_row)
            db.flush()
            tech_sheet_id_map[row.id] = clone_row.id
            counts["tech_sheets"] += 1

        for row in (
            db.query(TechSheetLink)
            .filter(TechSheetLink.campaign_id == source_campaign_id)
            .order_by(TechSheetLink.id.asc())
        ):
            mapped_invoice_item_id = invoice_item_id_map.get(row.invoice_item_id)
            mapped_tech_sheet_id = tech_sheet_id_map.get(row.tech_sheet_id)
            if not mapped_invoice_item_id or not mapped_tech_sheet_id:
                continue
            db.add(
                TechSheetLink(
                    tenant_id=row.tenant_id,
                    campaign_id=clone.id,
                    supplier_id=row.supplier_id,
                    invoice_item_id=mapped_invoice_item_id,
                    tech_sheet_id=mapped_tech_sheet_id,
                    score=row.score,
                    method=row.method,
                    created_at=row.created_at,
                )
            )
            counts["tech_links"] += 1

        for row in (
            db.query(InvoiceItemCharacteristicMatch)
            .filter(InvoiceItemCharacteristicMatch.campaign_id == source_campaign_id)
            .order_by(InvoiceItemCharacteristicMatch.id.asc())
        ):
            mapped_invoice_item_id = invoice_item_id_map.get(row.invoice_item_id)
            if not mapped_invoice_item_id:
                continue
            db.add(
                InvoiceItemCharacteristicMatch(
                    tenant_id=row.tenant_id,
                    campaign_id=clone.id,
                    invoice_item_id=mapped_invoice_item_id,
                    requested_product_name=row.requested_product_name,
                    requested_product_norm=row.requested_product_norm,
                    matched_product_name=row.matched_product_name,
                    threshold=row.threshold,
                    score=row.score,
                    matched_points=row.matched_points,
                    total_points=row.total_points,
                    points=_clone_json(row.points),
                    source=row.source,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
            )
            counts["char_matches"] += 1

        db.commit()

        warmed = False
        if warm_cache:
            owner = db.query(User).filter(User.id == clone.owner_user_id).first()
            if owner:
                warm_results_cache_for_campaign(clone.id, db, owner)
                warmed = True

        return {
            "source_campaign_id": source_campaign_id,
            "campaign_id": clone.id,
            "title": clone.title,
            "warmed_results_cache": warmed,
            **counts,
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Clone a campaign with extracted data.")
    parser.add_argument("--source-campaign-id", type=int, required=True)
    parser.add_argument("--title", default=None)
    parser.add_argument("--warm-cache", action="store_true")
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()

    summary = clone_campaign(
        source_campaign_id=args.source_campaign_id,
        title=args.title,
        warm_cache=args.warm_cache,
    )
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
