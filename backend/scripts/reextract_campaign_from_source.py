from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import config  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Campaign,
    CampaignAttachment,
    CampaignItem,
    InboundInvoice,
    InvoiceExtractionItem,
    Product,
    TechSheet,
    User,
)
from app.routes.campaigns import warm_results_cache_for_campaign  # noqa: E402
from app.routes.invoice_extractions import (  # noqa: E402
    _extract_invoice_attachment_like_components,
)
from app.routes.tech_sheets import extract_from_files  # noqa: E402
from app.schemas import TechSheetExtractPayload  # noqa: E402
from app.services.campaign_attachment_extraction import (  # noqa: E402
    extract_attachment_products_preview,
)
from app.services.invoice_extraction import (  # noqa: E402
    extract_line_items,
    match_products_hybrid,
    rebuild_characteristics_pair_cache,
)
from app.settings_service import get_user_embedding_thresholds  # noqa: E402


def _clone_json(value: Any) -> Any:
    return copy.deepcopy(value) if value is not None else None


def _simple_norm(text: str) -> str:
    return (
        str(text or "")
        .lower()
        .replace("Ã©", "e")
        .replace("Ã¨", "e")
        .replace("Ãª", "e")
        .replace("Ã«", "e")
        .replace("Ã ", "a")
        .replace("Ã¢", "a")
        .replace("Ã®", "i")
        .replace("Ã¯", "i")
        .replace("Ã´", "o")
        .replace("Ã¶", "o")
        .replace("Ã»", "u")
        .replace("Ã¼", "u")
        .replace("Ã§", "c")
    )


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", _simple_norm(text)).strip()


def _push_characteristic(target: dict[str, list[str]], raw_key: str, raw_value: str) -> None:
    key = str(raw_key or "").strip()
    value = str(raw_value or "").strip()
    if not key or not value:
        return
    bucket = target.setdefault(key, [])
    norm = value.lower().strip()
    if any(existing.lower().strip() == norm for existing in bucket):
        return
    bucket.append(value)


def _characteristics_from_dynamic(raw: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "")
        if isinstance(raw_value, list):
            for row in raw_value:
                if isinstance(row, dict):
                    _push_characteristic(out, key, str(row.get("value") or ""))
                else:
                    _push_characteristic(out, key, str(row))
        else:
            _push_characteristic(out, key, str(raw_value))
    return out


def _characteristics_from_structured_product(raw: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        values = entry.get("values")
        if isinstance(values, list):
            for value in values:
                _push_characteristic(out, key, str(value))
        elif values is not None:
            _push_characteristic(out, key, str(values))
    return out


def _merge_characteristics(maps: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in maps:
        for key, values in item.items():
            for value in values:
                _push_characteristic(out, key, value)
    return out


def _map_list_from_dynamic(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append({str(k): v for k, v in entry.items()})
    return out


def _texts_from_dynamic(raw: Any) -> list[str]:
    if isinstance(raw, list):
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
            else:
                text = str(entry or "").strip()
            if text:
                out.append(text)
        return out
    text = str(raw or "").strip()
    return [text] if text else []


def _accessory_display_line(accessory: dict[str, Any]) -> str:
    parts: list[str] = []
    qty = accessory.get("qty")
    item = str(accessory.get("item") or "").strip()
    details = str(accessory.get("details") or "").strip()
    material = str(accessory.get("material") or "").strip()
    group = str(accessory.get("group") or "").strip()
    if qty is not None and str(qty).strip():
        parts.append(f"{qty}x")
    if item:
        parts.append(item)
    if details:
        parts.append(details)
    if material:
        parts.append(material)
    if group:
        parts.append(group)
    return " - ".join(parts).strip()


def _accessory_lines_from_dynamic(raw: Any) -> list[str]:
    out: list[str] = []
    for accessory in _map_list_from_dynamic(raw):
        line = _accessory_display_line(accessory)
        if line and line != "-":
            out.append(line)
    return out


def _requested_characteristics_from_extracted_row(row: dict[str, Any]) -> dict[str, list[str]]:
    out = _merge_characteristics(
        [
            _characteristics_from_dynamic(row.get("characteristics")),
        ]
    )
    for line in _accessory_lines_from_dynamic(row.get("accessoires_structures")):
        _push_characteristic(out, "accessoires_structures", line)
    for line in _texts_from_dynamic(row.get("exigences")):
        _push_characteristic(out, "exigences", line)
    return out


def _characteristics_to_storage_map(
    values: dict[str, list[str]] | None,
) -> dict[str, list[dict[str, str]]] | None:
    if not values:
        return None
    out: dict[str, list[dict[str, str]]] = {}
    for key, rows in values.items():
        clean_key = str(key or "").strip()
        if not clean_key:
            continue
        seen: set[str] = set()
        bucket: list[dict[str, str]] = []
        for raw_value in rows:
            value = str(raw_value or "").strip()
            if not value:
                continue
            token = value.lower()
            if token in seen:
                continue
            seen.add(token)
            bucket.append({"value": value})
        if bucket:
            out[clean_key] = bucket
    return out or None


def _preview_rows(preview: dict[str, Any] | None, attachment_name: str) -> list[dict[str, Any]]:
    if not isinstance(preview, dict):
        return []
    preview_doc_type = str(preview.get("doc_type") or "").strip() or None
    rows: list[dict[str, Any]] = []
    products = preview.get("products")
    if isinstance(products, list) and products:
        for raw_product in products:
            if not isinstance(raw_product, dict):
                continue
            product = {str(k): v for k, v in raw_product.items()}
            merged_chars = _characteristics_from_structured_product(product.get("characteristics"))
            rows.append(
                {
                    "attachment_name": attachment_name,
                    "product_name": str(product.get("name") or "").strip(),
                    "description": str(product.get("description") or "").strip(),
                    "quantity": product.get("quantity"),
                    "embedding_max_score": float(product.get("quality_score") or 0.0),
                    "doc_type": preview_doc_type,
                    "characteristics": _characteristics_to_storage_map(merged_chars),
                    "accessoires_structures": _map_list_from_dynamic(
                        product.get("accessoires_structures")
                    ),
                    "exigences": product.get("exigences") or [],
                }
            )
        return rows
    return rows


def _upsert_product(
    db,
    *,
    tenant_id: int,
    owner_user_id: int,
    product_index: dict[str, Product],
    product_name: str,
    description: str,
    characteristics_storage: dict[str, list[dict[str, str]]] | None,
    embedding_score: float | None,
) -> Product:
    key = _name_key(product_name)
    existing = product_index.get(key)
    if existing is None:
        existing = Product(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=product_name,
            description=description or None,
            characteristics=_clone_json(characteristics_storage),
            embedding_score=embedding_score if (embedding_score or 0.0) > 0 else None,
        )
        db.add(existing)
        db.flush()
        product_index[key] = existing
        return existing

    if description and not str(existing.description or "").strip():
        existing.description = description
    if characteristics_storage and not (isinstance(existing.characteristics, dict) and existing.characteristics):
        existing.characteristics = _clone_json(characteristics_storage)
    current_emb = float(existing.embedding_score or 0.0)
    next_emb = float(embedding_score or 0.0)
    if next_emb > current_emb:
        existing.embedding_score = next_emb
    return existing


def rebuild_campaign_from_source(
    source_campaign_id: int,
    title: str | None = None,
    warm_cache: bool = True,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        source = db.query(Campaign).filter(Campaign.id == source_campaign_id).first()
        if not source:
            raise RuntimeError(f"Source campaign {source_campaign_id} not found")
        owner = db.query(User).filter(User.id == source.owner_user_id).first()
        if owner is None:
            raise RuntimeError(f"Owner user {source.owner_user_id} not found")

        now = datetime.now(UTC).replace(tzinfo=None)
        new_title = (title or f"{source.title} REEXTRACT {now.strftime('%Y%m%d_%H%M%S')}").strip()
        campaign = Campaign(
            tenant_id=source.tenant_id,
            owner_user_id=source.owner_user_id,
            title=new_title,
            subject=source.subject,
            message_html=source.message_html,
            priority=source.priority,
            status=source.status,
            sent_at=source.sent_at,
        )
        db.add(campaign)
        db.flush()

        source_attachments = (
            db.query(CampaignAttachment)
            .filter(CampaignAttachment.campaign_id == source_campaign_id)
            .order_by(CampaignAttachment.id.asc())
            .all()
        )
        source_invoices = (
            db.query(InboundInvoice)
            .filter(InboundInvoice.campaign_id == source_campaign_id)
            .order_by(InboundInvoice.id.asc())
            .all()
        )
        source_sheets = (
            db.query(TechSheet)
            .filter(TechSheet.campaign_id == source_campaign_id)
            .order_by(TechSheet.id.asc())
            .all()
        )

        attachment_rows: list[CampaignAttachment] = []
        for row in source_attachments:
            clone = CampaignAttachment(
                tenant_id=row.tenant_id,
                campaign_id=campaign.id,
                file_name=row.file_name,
                file_path=row.file_path,
                file_size=row.file_size,
                created_at=row.created_at,
            )
            db.add(clone)
            attachment_rows.append(clone)

        invoice_rows: list[InboundInvoice] = []
        for row in source_invoices:
            clone = InboundInvoice(
                tenant_id=row.tenant_id,
                campaign_id=campaign.id,
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
            db.add(clone)
            invoice_rows.append(clone)

        db.flush()

        thresholds = get_user_embedding_thresholds(db, owner.tenant_id, owner.id)
        extracted_preview_counts: list[dict[str, Any]] = []
        requested_rows: list[dict[str, Any]] = []
        for row in attachment_rows:
            preview = extract_attachment_products_preview(
                row.file_path,
                thresholds.extraction_threshold,
            )
            row.parse_engine = str(preview.get("engine") or "document_ai_ocr")
            row.parse_score = float(preview.get("avg_regex_score") or 0.0)
            row.extraction_preview = preview
            row.extracted_at = datetime.utcnow()
            preview_rows = _preview_rows(preview, row.file_name)
            requested_rows.extend(preview_rows)
            extracted_preview_counts.append(
                {
                    "file_name": row.file_name,
                    "products": len(preview_rows),
                    "unassigned": len(preview.get("unassigned") or []),
                }
            )

        product_rows = (
            db.query(Product)
            .filter(Product.tenant_id == owner.tenant_id)
            .order_by(Product.id.asc())
            .all()
        )
        product_index = {
            _name_key(str(product.name or "")): product
            for product in product_rows
            if _name_key(str(product.name or ""))
        }

        merged_items_by_product_id: dict[int, dict[str, Any]] = {}
        for row in requested_rows:
            product_name = str(row.get("product_name") or "").strip()
            if not product_name:
                continue
            description = str(row.get("description") or "").strip()
            requested_chars = _requested_characteristics_from_extracted_row(row)
            requested_chars_storage = _characteristics_to_storage_map(requested_chars)
            embedding_score = float(row.get("embedding_max_score") or 0.0)
            product = _upsert_product(
                db,
                tenant_id=owner.tenant_id,
                owner_user_id=owner.id,
                product_index=product_index,
                product_name=product_name,
                description=description,
                characteristics_storage=requested_chars_storage,
                embedding_score=embedding_score,
            )
            existing = merged_items_by_product_id.get(product.id)
            if existing is None:
                merged_items_by_product_id[product.id] = {
                    "product_id": product.id,
                    "requested_qty": row.get("quantity"),
                    "requested_description": description or None,
                    "requested_characteristics": requested_chars_storage,
                    "requested_embedding_score": embedding_score if embedding_score > 0 else None,
                }
                continue

            current_desc = str(existing.get("requested_description") or "").strip()
            if description and not current_desc:
                existing["requested_description"] = description

            merged_storage = _characteristics_to_storage_map(
                _merge_characteristics(
                    [
                        _characteristics_from_dynamic(existing.get("requested_characteristics")),
                        requested_chars,
                    ]
                )
            )
            existing["requested_characteristics"] = merged_storage

            qty = row.get("quantity")
            if qty is not None:
                existing["requested_qty"] = qty

            current_emb = float(existing.get("requested_embedding_score") or 0.0)
            if embedding_score > current_emb:
                existing["requested_embedding_score"] = embedding_score

        for payload in merged_items_by_product_id.values():
            db.add(
                CampaignItem(
                    tenant_id=owner.tenant_id,
                    campaign_id=campaign.id,
                    product_id=payload["product_id"],
                    requested_qty=payload.get("requested_qty"),
                    requested_description=payload.get("requested_description"),
                    requested_characteristics=_clone_json(payload.get("requested_characteristics")),
                    requested_embedding_score=payload.get("requested_embedding_score"),
                )
            )

        db.flush()

        product_rows_for_match = (
            db.query(Product)
            .join(CampaignItem, CampaignItem.product_id == Product.id)
            .filter(
                CampaignItem.campaign_id == campaign.id,
                CampaignItem.tenant_id == owner.tenant_id,
                Product.tenant_id == owner.tenant_id,
            )
            .all()
        )

        extracted_invoice_counts: list[dict[str, Any]] = []
        all_item_ids: list[int] = []
        for invoice in invoice_rows:
            items = extract_line_items(invoice, db)
            if items:
                match_products_hybrid(
                    items,
                    product_rows_for_match,
                    threshold=0.70,
                    embed_threshold=thresholds.confirm_threshold,
                    embed_min_threshold=thresholds.verify_threshold,
                    characteristics_match_threshold=thresholds.characteristics_threshold,
                    db=db,
                )
                if bool(getattr(config, "OPENAI_INVOICE_COMPONENTS_PRECOMPUTE_ON_EXTRACT", True)):
                    for item in items:
                        _extract_invoice_attachment_like_components(item)
                for item in items:
                    db.add(item)
                db.flush()
                item_ids = [item.id for item in items if item.id is not None]
                if item_ids:
                    rebuild_characteristics_pair_cache(
                        db=db,
                        tenant_id=owner.tenant_id,
                        campaign_id=campaign.id,
                        threshold=float(thresholds.characteristics_threshold),
                        item_ids=item_ids,
                    )
                    all_item_ids.extend(item_ids)
            extracted_invoice_counts.append(
                {
                    "file_name": invoice.file_name,
                    "items": len(items),
                }
            )

        tech_sheet_groups: dict[str | None, list[str]] = defaultdict(list)
        for row in source_sheets:
            tech_sheet_groups[(row.sender_email or "").strip().lower() or None].append(row.file_path)

        extracted_tech_sheet_counts: list[dict[str, Any]] = []
        for sender_email, file_paths in tech_sheet_groups.items():
            payload = TechSheetExtractPayload(
                campaign_id=campaign.id,
                sender_email=sender_email,
                file_paths=file_paths,
            )
            sheets = extract_from_files(payload, db=db, current_user=owner)
            extracted_tech_sheet_counts.append(
                {
                    "sender_email": sender_email,
                    "count": len(sheets),
                }
            )

        db.commit()

        warmed = False
        if warm_cache:
            warm_results_cache_for_campaign(campaign.id, db, owner)
            warmed = True

        return {
            "source_campaign_id": source_campaign_id,
            "campaign_id": campaign.id,
            "title": campaign.title,
            "warmed_results_cache": warmed,
            "attachments": len(attachment_rows),
            "requested_products": len(merged_items_by_product_id),
            "invoices": len(invoice_rows),
            "invoice_items": len(all_item_ids),
            "tech_sheets": sum(len(v) for v in tech_sheet_groups.values()),
            "attachment_preview_counts": extracted_preview_counts,
            "invoice_extract_counts": extracted_invoice_counts,
            "tech_sheet_extract_counts": extracted_tech_sheet_counts,
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a fresh campaign and re-extract from source PDFs.")
    parser.add_argument("--source-campaign-id", type=int, required=True)
    parser.add_argument("--title", default=None)
    parser.add_argument("--no-warm-cache", action="store_true")
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()

    summary = rebuild_campaign_from_source(
        source_campaign_id=args.source_campaign_id,
        title=args.title,
        warm_cache=not args.no_warm_cache,
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
