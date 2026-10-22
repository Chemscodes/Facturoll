from __future__ import annotations

import argparse
import copy
import json
import re
import sys
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
    EmbeddingJob,
    EmbeddingJobStatus,
    ExtractionStatus,
    InboundInvoice,
    InvoiceExtractionItem,
    Product,
    User,
)
from app.embedding_worker import run_worker  # noqa: E402
from app.routes.campaigns import warm_results_cache_for_campaign  # noqa: E402
from app.routes.invoice_extractions import (  # noqa: E402
    _extract_invoice_attachment_like_components,
)
from app.services.campaign_attachment_extraction import (  # noqa: E402
    extract_attachment_products_preview,
)
from app.services.invoice_extraction import (  # noqa: E402
    extract_line_items,
    match_products_hybrid,
    rebuild_characteristics_pair_cache,
)
from app.settings_service import get_user_embedding_thresholds  # noqa: E402


_SUPPORTED_INVOICE_SUFFIXES = {".pdf", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".webp"}


def _clone_json(value: Any) -> Any:
    return copy.deepcopy(value) if value is not None else None


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(text or "").lower()).strip()


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


def _map_list_from_dynamic(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append({str(k): v for k, v in entry.items()})
    return out


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


def _requested_characteristics_from_product_payload(product: dict[str, Any]) -> dict[str, list[str]]:
    out = _characteristics_from_structured_product(product.get("characteristics"))
    for accessory in _map_list_from_dynamic(product.get("accessoires_structures")):
        line = _accessory_display_line(accessory)
        if line and line != "-":
            _push_characteristic(out, "accessoires_structures", line)
    for line in _texts_from_dynamic(product.get("exigences")):
        _push_characteristic(out, "exigences", line)
    return out


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


def _resolve_owner(db, tenant_id: int, owner_user_id: int | None) -> User:
    if owner_user_id is not None:
        row = db.query(User).filter(User.id == owner_user_id, User.tenant_id == tenant_id).first()
        if row is None:
            raise RuntimeError(f"owner_user_id={owner_user_id} not found for tenant_id={tenant_id}")
        return row
    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant_id)
        .order_by(User.is_admin.desc(), User.id.asc())
        .first()
    )
    if owner is None:
        raise RuntimeError(f"No users found for tenant_id={tenant_id}")
    return owner


def _list_invoice_files(invoices_dir: Path) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    skipped: list[dict[str, str]] = []
    for path in invoices_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_INVOICE_SUFFIXES:
            continue
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            skipped.append(
                {
                    "file_name": path.name,
                    "reason": "image_not_supported_by_invoice_parser",
                }
            )
            continue
        if suffix == ".pdf":
            try:
                header = path.read_bytes()[:8]
            except Exception:
                header = b""
            if not header.startswith(b"%PDF-"):
                skipped.append(
                    {
                        "file_name": path.name,
                        "reason": "invalid_pdf_header",
                    }
                )
                continue
        files.append(path)
    files.sort(key=lambda p: (p.name.lower(), str(p).lower()))
    return files, skipped


def _embedding_queue_count(db) -> int:
    from sqlalchemy import func

    return int(
        db.query(func.count(EmbeddingJob.id))
        .filter(EmbeddingJob.status == EmbeddingJobStatus.QUEUED)
        .scalar()
        or 0
    )


def _drain_embedding_jobs(max_rounds: int = 8) -> int:
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        run_worker(
            batch_size=500,
            poll_interval=0.05,
            once=True,
            stuck_timeout_seconds=300,
        )
        with SessionLocal() as db:
            queued = _embedding_queue_count(db)
        if queued <= 0:
            break
    return rounds


def run_campaign(
    *,
    requested_file: Path,
    invoices_dir: Path,
    tenant_id: int,
    owner_user_id: int | None,
    sender_email: str,
    title: str | None,
    warm_cache: bool,
) -> dict[str, Any]:
    requested_file = requested_file.resolve()
    invoices_dir = invoices_dir.resolve()

    if not requested_file.exists():
        raise RuntimeError(f"Requested attachment not found: {requested_file}")
    if not invoices_dir.exists() or not invoices_dir.is_dir():
        raise RuntimeError(f"Invoices dir not found: {invoices_dir}")

    db = SessionLocal()
    try:
        owner = _resolve_owner(db, tenant_id=tenant_id, owner_user_id=owner_user_id)
        thresholds = get_user_embedding_thresholds(db, owner.tenant_id, owner.id)
        now = datetime.now(UTC).replace(tzinfo=None)
        campaign = Campaign(
            tenant_id=owner.tenant_id,
            owner_user_id=owner.id,
            title=(title or f"TEST PATHS {now.strftime('%Y%m%d_%H%M%S')}").strip(),
            subject="Test extraction",
            message_html="<p>Test extraction auto</p>",
            priority=0,
        )
        db.add(campaign)
        db.flush()

        attachment_row = CampaignAttachment(
            tenant_id=owner.tenant_id,
            campaign_id=campaign.id,
            file_name=requested_file.name,
            file_path=str(requested_file),
            file_size=int(requested_file.stat().st_size),
        )
        db.add(attachment_row)
        db.flush()

        preview = extract_attachment_products_preview(
            str(requested_file),
            thresholds.extraction_threshold,
        )
        attachment_row.parse_engine = str(preview.get("engine") or "document_ai_ocr")
        attachment_row.parse_score = float(preview.get("avg_regex_score") or 0.0)
        attachment_row.extraction_preview = preview
        attachment_row.extracted_at = datetime.utcnow()

        products_payload = preview.get("products") if isinstance(preview, dict) else []
        if not isinstance(products_payload, list):
            products_payload = []

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

        campaign_products_created = 0
        for raw_product in products_payload:
            if not isinstance(raw_product, dict):
                continue
            product_name = str(raw_product.get("name") or "").strip()
            if not product_name:
                continue
            description = str(raw_product.get("description") or "").strip()
            quality_score = float(raw_product.get("quality_score") or 0.0)
            chars_map = _requested_characteristics_from_product_payload(raw_product)
            chars_storage = _characteristics_to_storage_map(chars_map)
            product = _upsert_product(
                db,
                tenant_id=owner.tenant_id,
                owner_user_id=owner.id,
                product_index=product_index,
                product_name=product_name,
                description=description,
                characteristics_storage=chars_storage,
                embedding_score=quality_score,
            )
            existing_item = (
                db.query(CampaignItem)
                .filter(
                    CampaignItem.tenant_id == owner.tenant_id,
                    CampaignItem.campaign_id == campaign.id,
                    CampaignItem.product_id == product.id,
                )
                .first()
            )
            if existing_item is not None:
                continue
            db.add(
                CampaignItem(
                    tenant_id=owner.tenant_id,
                    campaign_id=campaign.id,
                    product_id=product.id,
                    requested_qty=raw_product.get("quantity"),
                    requested_description=description or None,
                    requested_characteristics=_clone_json(chars_storage),
                    requested_embedding_score=quality_score if quality_score > 0 else None,
                )
            )
            campaign_products_created += 1

        db.flush()

        invoice_files, skipped_invoice_files = _list_invoice_files(invoices_dir)
        invoice_rows: list[InboundInvoice] = []
        for file_path in invoice_files:
            invoice = InboundInvoice(
                tenant_id=owner.tenant_id,
                campaign_id=campaign.id,
                sender_email=sender_email,
                sender_name=None,
                sender_phone=None,
                parse_engine=None,
                parse_score=None,
                subject=f"Auto import {file_path.name}",
                message_id=f"manual-{campaign.id}-{file_path.name}",
                file_name=file_path.name,
                file_path=str(file_path),
                received_at=datetime.utcnow(),
            )
            db.add(invoice)
            invoice_rows.append(invoice)

        db.flush()

        campaign_products = (
            db.query(Product)
            .join(CampaignItem, CampaignItem.product_id == Product.id)
            .filter(
                CampaignItem.campaign_id == campaign.id,
                CampaignItem.tenant_id == owner.tenant_id,
                Product.tenant_id == owner.tenant_id,
            )
            .all()
        )

        invoice_summaries: list[dict[str, Any]] = []
        total_item_ids: list[int] = []

        for invoice in invoice_rows:
            items: list[Any] = []
            item_ids: list[int] = []
            try:
                items = extract_line_items(invoice, db)
                if items:
                    match_products_hybrid(
                        items,
                        campaign_products,
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
                    item_ids = [int(item.id) for item in items if item.id is not None]
                    if item_ids:
                        rebuild_characteristics_pair_cache(
                            db=db,
                            tenant_id=owner.tenant_id,
                            campaign_id=campaign.id,
                            threshold=float(thresholds.characteristics_threshold),
                            item_ids=item_ids,
                        )
                        total_item_ids.extend(item_ids)
                invoice_summaries.append(
                    {
                        "file_name": invoice.file_name,
                        "items": len(items or []),
                        "item_names": [str(getattr(i, "product_name", "") or "") for i in (items or [])[:10]],
                    }
                )
            except Exception as exc:  # pragma: no cover - operational path
                invoice_summaries.append(
                    {
                        "file_name": invoice.file_name,
                        "items": 0,
                        "item_names": [],
                        "error": str(exc),
                    }
                )

        rematch_rounds = 0
        rematched_items = 0
        queue_before_rematch = _embedding_queue_count(db)
        if total_item_ids:
            # Ensure embeddings are fully materialized, then run a second pass so
            # notes contain emb/gpt scores used by Results filters.
            rematch_rounds = _drain_embedding_jobs(max_rounds=10)
            rematch_rows = (
                db.query(InvoiceExtractionItem)
                .filter(
                    InvoiceExtractionItem.campaign_id == campaign.id,
                    InvoiceExtractionItem.tenant_id == owner.tenant_id,
                    InvoiceExtractionItem.id.in_(total_item_ids),
                )
                .all()
            )
            if rematch_rows:
                for row in rematch_rows:
                    row.status = ExtractionStatus.TO_VERIFY
                    row.corrected_product_name = None
                    row.similarity_score = None
                    row.similarity_points = None
                    row.notes = None
                match_products_hybrid(
                    rematch_rows,
                    campaign_products,
                    threshold=0.70,
                    embed_threshold=thresholds.confirm_threshold,
                    embed_min_threshold=thresholds.verify_threshold,
                    characteristics_match_threshold=thresholds.characteristics_threshold,
                    db=db,
                )
                for row in rematch_rows:
                    db.add(row)
                db.commit()
                rematched_items = len(rematch_rows)
            else:
                db.commit()

        # Keep pair cache in sync with final (post-rematch) values.
        if total_item_ids:
            rebuild_characteristics_pair_cache(
                db=db,
                tenant_id=owner.tenant_id,
                campaign_id=campaign.id,
                threshold=float(thresholds.characteristics_threshold),
                item_ids=total_item_ids,
            )
        db.commit()

        warmed = False
        if warm_cache:
            warm_results_cache_for_campaign(campaign.id, db, owner)
            warmed = True

        return {
            "campaign_id": campaign.id,
            "campaign_title": campaign.title,
            "tenant_id": owner.tenant_id,
            "owner_user_id": owner.id,
            "requested_attachment": str(requested_file),
            "invoices_dir": str(invoices_dir),
            "requested_products_created": campaign_products_created,
            "attachment_products_detected": len(products_payload),
            "attachment_unassigned": len(preview.get("unassigned") or []) if isinstance(preview, dict) else 0,
            "invoices_imported": len(invoice_rows),
            "invoices_skipped": skipped_invoice_files,
            "invoice_items_extracted": len(total_item_ids),
            "embedding_queue_before_rematch": queue_before_rematch,
            "embedding_rematch_rounds": rematch_rounds,
            "invoice_items_rematched": rematched_items,
            "invoice_extract_counts": invoice_summaries,
            "warmed_results_cache": warmed,
            "generated_at": datetime.utcnow().isoformat(),
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a full local campaign test from file paths.")
    parser.add_argument("--requested-file", required=True)
    parser.add_argument("--invoices-dir", required=True)
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--owner-user-id", type=int, default=None)
    parser.add_argument("--sender-email", default="manual.import@local.test")
    parser.add_argument("--title", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--no-warm-cache", action="store_true")
    args = parser.parse_args()

    summary = run_campaign(
        requested_file=Path(args.requested_file),
        invoices_dir=Path(args.invoices_dir),
        tenant_id=int(args.tenant_id),
        owner_user_id=args.owner_user_id,
        sender_email=str(args.sender_email).strip() or "manual.import@local.test",
        title=args.title,
        warm_cache=not bool(args.no_warm_cache),
    )

    output_dir = Path("reports/campaign_test_runs")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_path = output_dir / f"campaign_paths_test_{summary['campaign_id']}_{stamp}.json"
    target = Path(args.output_json) if args.output_json else default_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report={target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
