from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pypdf import PdfReader

from app import config
from app.db import SessionLocal
from app.models import CampaignAttachment, InboundInvoice, InvoiceExtractionItem
from app.routes.campaigns import _build_match_comparison_payload, _build_match_unit_payload
from app.routes.invoice_extractions import _pick_requested_reference
from app.services.openai_chat import call_chat_completion_json_with_retry


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _mime_from_name(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "image/png"


def _build_data_url(image_bytes: bytes, mime: str) -> str:
    import base64

    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _extract_pdf_page_images(file_path: str, max_pages: int) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    reader = PdfReader(file_path)
    pages = reader.pages[: max(1, int(max_pages))]
    for idx, page in enumerate(pages, start=1):
        try:
            images = list(page.images)
        except Exception:
            images = []
        if not images:
            continue
        best = max(images, key=lambda img: len(getattr(img, "data", b"") or b""))
        name = str(getattr(best, "name", "") or f"page_{idx}.png")
        data = bytes(getattr(best, "data", b"") or b"")
        if not data:
            continue
        out.append((name, data))
    return out


def _extract_input_images(file_path: str, max_pages: int) -> list[tuple[str, bytes]]:
    path = Path(file_path)
    if not path.exists():
        return []
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return [(path.name, path.read_bytes())]
    if suffix == ".pdf":
        return _extract_pdf_page_images(str(path), max_pages=max_pages)
    return []


def _call_gpt4_vision_requested_products(
    *,
    image_inputs: list[tuple[str, bytes]],
    model: str,
    fallback_model: str | None,
    attachment_id: int,
    file_name: str,
) -> tuple[list[dict[str, Any]], str | None]:
    if not image_inputs:
        return [], None

    system_prompt = (
        "Tu extrais des lignes produit demandees depuis un appel d'offre. "
        "Mode strict_produit: retourne uniquement les produits explicites, sans invention. "
        "Si ambigu, mets null."
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "packaging": {"type": ["string", "null"]},
                        "dimensions": {"type": ["string", "null"]},
                        "size": {"type": ["string", "null"]},
                        "weight": {"type": ["string", "null"]},
                        "vat_rate": {"type": ["number", "null"]},
                        "confidence": {"type": ["number", "null"]},
                    },
                    "required": [
                        "name",
                        "quantity",
                        "unit",
                        "packaging",
                        "dimensions",
                        "size",
                        "weight",
                        "vat_rate",
                        "confidence",
                    ],
                },
            }
        },
        "required": ["products"],
    }

    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "task": "strict_requested_products_extraction_from_vision",
                    "attachment_id": attachment_id,
                    "file_name": file_name,
                    "doc_type_hint": "strict_produit",
                },
                ensure_ascii=False,
            ),
        }
    ]
    for name, data in image_inputs:
        mime = _mime_from_name(name)
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _build_data_url(data, mime),
                    "detail": "high",
                },
            }
        )

    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "requested_products_strict_vision",
                "strict": True,
                "schema": schema,
            },
        },
    }
    parsed, used_model = call_chat_completion_json_with_retry(
        operation="compare_requested_offered_requested_vision_test",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=max(30, int(getattr(config, "OPENAI_ATTACHMENT_TIMEOUT_SECONDS", 120) or 120)),
        retry_attempts=max(1, int(getattr(config, "OPENAI_ATTACHMENT_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(0.2, float(getattr(config, "OPENAI_ATTACHMENT_RETRY_BACKOFF_SECONDS", 1.0) or 1.0)),
        metadata={"attachment_id": attachment_id, "images": len(image_inputs)},
    )
    raw_rows = parsed.get("products") if isinstance(parsed, dict) and isinstance(parsed.get("products"), list) else []
    out: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        name = _clean_text(row.get("name"))
        if not name:
            continue
        out.append(
            {
                "name": name,
                "quantity": _to_float(row.get("quantity")),
                "unit": _clean_text(row.get("unit")),
                "packaging": _clean_text(row.get("packaging")),
                "dimensions": _clean_text(row.get("dimensions")),
                "size": _clean_text(row.get("size")),
                "weight": _clean_text(row.get("weight")),
                "vat_rate": _to_float(row.get("vat_rate")),
                "confidence": _to_float(row.get("confidence")),
            }
        )
    return out, used_model


def _call_gpt4_vision_offered_rows(
    *,
    image_inputs: list[tuple[str, bytes]],
    model: str,
    fallback_model: str | None,
    invoice_id: int,
) -> tuple[list[dict[str, Any]], str | None]:
    if not image_inputs:
        return [], None

    system_prompt = (
        "Extract strict invoice line items from provided invoice page images only. "
        "Ignore headers/totals/payment lines. Keep invoice order. "
        "Do not infer missing values aggressively. "
        "Return JSON only."
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": ["number", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                        "line_total": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "packaging": {"type": ["string", "null"]},
                        "weight": {"type": ["string", "null"]},
                        "dimensions": {"type": ["string", "null"]},
                        "vat_rate": {"type": ["number", "null"]},
                        "confidence": {"type": ["number", "null"]},
                    },
                    "required": [
                        "name",
                        "quantity",
                        "unit_price",
                        "line_total",
                        "unit",
                        "packaging",
                        "weight",
                        "dimensions",
                        "vat_rate",
                        "confidence",
                    ],
                },
            }
        },
        "required": ["rows"],
    }

    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "task": "strict_invoice_extraction_from_vision",
                    "invoice_id": invoice_id,
                },
                ensure_ascii=False,
            ),
        }
    ]
    for name, data in image_inputs:
        mime = _mime_from_name(name)
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _build_data_url(data, mime),
                    "detail": "high",
                },
            }
        )

    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_strict_vision_rows",
                "strict": True,
                "schema": schema,
            },
        },
    }
    parsed, used_model = call_chat_completion_json_with_retry(
        operation="compare_requested_offered_offered_vision_test",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=max(30, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45)),
        retry_attempts=max(1, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(0.2, float(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0) or 1.0)),
        metadata={"invoice_id": invoice_id, "images": len(image_inputs)},
    )
    raw_rows = parsed.get("rows") if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list) else []
    out: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        name = _clean_text(row.get("name"))
        if not name:
            continue
        out.append(
            {
                "name": name,
                "quantity": _to_float(row.get("quantity")),
                "unit_price": _to_float(row.get("unit_price")),
                "line_total": _to_float(row.get("line_total")),
                "unit": _clean_text(row.get("unit")),
                "packaging": _clean_text(row.get("packaging")),
                "dimensions": _clean_text(row.get("dimensions")),
                "size": None,
                "weight": _clean_text(row.get("weight")),
                "vat_rate": _to_float(row.get("vat_rate")),
                "confidence": _to_float(row.get("confidence")),
            }
        )
    return out, used_model


def _build_offered_rows_from_db(
    *,
    db,
    campaign_id: int,
    max_invoices: int,
) -> list[dict[str, Any]]:
    invoice_rows = (
        db.query(InboundInvoice)
        .filter(InboundInvoice.campaign_id == campaign_id)
        .order_by(InboundInvoice.id.asc())
        .limit(max(1, int(max_invoices)))
        .all()
    )
    invoice_by_id = {int(inv.id): inv for inv in invoice_rows}
    invoice_ids = list(invoice_by_id.keys())

    items_query = (
        db.query(InvoiceExtractionItem)
        .filter(InvoiceExtractionItem.campaign_id == campaign_id)
        .order_by(InvoiceExtractionItem.invoice_id.asc(), InvoiceExtractionItem.id.asc())
    )
    if invoice_ids:
        items_query = items_query.filter(InvoiceExtractionItem.invoice_id.in_(invoice_ids))
    items = items_query.all()
    if not items:
        return []

    # If invoices were not linked via InboundInvoice.campaign_id, keep first N invoice ids
    # discovered from extraction items.
    max_inv = max(1, int(max_invoices))
    seen_inv_ids: list[int] = []
    seen_set: set[int] = set()
    for it in items:
        inv_id = int(getattr(it, "invoice_id", 0) or 0)
        if inv_id <= 0 or inv_id in seen_set:
            continue
        seen_set.add(inv_id)
        seen_inv_ids.append(inv_id)
        if len(seen_inv_ids) >= max_inv:
            break
    allowed_invoice_ids = set(seen_inv_ids)
    items = [it for it in items if int(getattr(it, "invoice_id", 0) or 0) in allowed_invoice_ids]

    # Load invoice metadata for file name when missing from first query path.
    missing_meta_ids = [iid for iid in allowed_invoice_ids if iid not in invoice_by_id]
    if missing_meta_ids:
        extra_invoices = (
            db.query(InboundInvoice)
            .filter(InboundInvoice.id.in_(missing_meta_ids))
            .all()
        )
        for inv in extra_invoices:
            invoice_by_id[int(inv.id)] = inv

    out: list[dict[str, Any]] = []
    idx_by_invoice: dict[int, int] = {}
    for it in items:
        invoice_id = int(it.invoice_id)
        inv = invoice_by_id.get(invoice_id)
        idx_by_invoice[invoice_id] = idx_by_invoice.get(invoice_id, 0) + 1
        strict = it.strict_components_payload if isinstance(it.strict_components_payload, dict) else {}
        name = (
            str(getattr(it, "corrected_product_name", "") or "").strip()
            or str(getattr(it, "product_name", "") or "").strip()
        )
        if not name:
            continue
        out.append(
            {
                "invoice_id": invoice_id,
                "invoice_file": str(getattr(inv, "file_name", "") or "").strip() if inv is not None else None,
                "row_idx": idx_by_invoice[invoice_id],
                "name": name,
                "quantity": _to_float(
                    getattr(it, "corrected_quantity", None)
                    if getattr(it, "corrected_quantity", None) is not None
                    else getattr(it, "quantity", None)
                ),
                "unit_price": _to_float(
                    getattr(it, "corrected_unit_price", None)
                    if getattr(it, "corrected_unit_price", None) is not None
                    else getattr(it, "unit_price", None)
                ),
                "line_total": _to_float(strict.get("line_total")),
                "unit": _clean_text(strict.get("unit")),
                "packaging": _clean_text(strict.get("packaging")),
                "dimensions": _clean_text(strict.get("dimensions")),
                "size": _clean_text(strict.get("size")),
                "weight": _clean_text(strict.get("weight")),
                "vat_rate": _to_float(strict.get("vat_rate")),
                "confidence": _to_float(strict.get("confidence")),
            }
        )
    return out


def _requested_line_text_from_row(row: dict[str, Any]) -> str:
    chunks = [
        str(row.get("product_name") or "").strip(),
        str(row.get("requested_description") or "").strip(),
    ]
    rc = row.get("requested_characteristics")
    if isinstance(rc, dict):
        for value in rc.values():
            text = str(value or "").strip()
            if text:
                chunks.append(text)
    return " | ".join([c for c in chunks if c])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare requested vs offered using GPT-4 vision extraction on both sides."
    )
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--model", type=str, default="gpt-4o")
    parser.add_argument("--fallback-model", type=str, default="")
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--max-invoices", type=int, default=20)
    parser.add_argument("--attachment-id", type=int, default=0)
    parser.add_argument("--requested-vision-only", action="store_true")
    parser.add_argument("--only-matched", action="store_true")
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY missing")
        return 1

    model = str(args.model or "gpt-4o").strip() or "gpt-4o"
    fallback = str(args.fallback_model or "").strip() or None

    db = SessionLocal()
    try:
        campaign_id = int(args.campaign_id)

        attachment_query = (
            db.query(CampaignAttachment)
            .filter(CampaignAttachment.campaign_id == campaign_id)
            .order_by(CampaignAttachment.id.asc())
        )
        if int(args.attachment_id or 0) > 0:
            attachment_query = attachment_query.filter(CampaignAttachment.id == int(args.attachment_id))
        attachments = attachment_query.all()
        if not attachments:
            print("NO_REQUESTED_ATTACHMENTS")
            return 1

        requested_candidates: list[dict[str, Any]] = []
        requested_vision_rows: list[dict[str, Any]] = []
        requested_model_used: str | None = None
        req_idx = 0
        for att in attachments:
            file_path = str(getattr(att, "file_path", "") or "").strip()
            if not file_path or not Path(file_path).exists():
                continue
            images = _extract_input_images(file_path, max_pages=max(1, int(args.max_pages)))
            if not images:
                continue
            products, used_model = _call_gpt4_vision_requested_products(
                image_inputs=images,
                model=model,
                fallback_model=fallback,
                attachment_id=int(att.id),
                file_name=str(getattr(att, "file_name", "") or "").strip(),
            )
            if used_model:
                requested_model_used = used_model
            for p in products:
                req_idx += 1
                name = str(p.get("name") or "").strip()
                req_row = {
                    "product_id": req_idx,
                    "product_name": name,
                    "requested_description": None,
                    "requested_characteristics": {
                        "quantity": p.get("quantity"),
                        "unit": p.get("unit"),
                        "packaging": p.get("packaging"),
                        "dimensions": p.get("dimensions"),
                        "size": p.get("size"),
                        "weight": p.get("weight"),
                        "vat_rate": p.get("vat_rate"),
                    },
                    "attachment_id": int(att.id),
                    "attachment_file": str(getattr(att, "file_name", "") or "").strip(),
                }
                requested_candidates.append(req_row)
                requested_vision_rows.append(
                    {
                        "attachment_id": int(att.id),
                        "attachment_file": str(getattr(att, "file_name", "") or "").strip(),
                        **p,
                    }
                )

        if not requested_candidates:
            print("NO_REQUESTED_PRODUCTS_FROM_VISION")
            return 1

        offered_vision_rows: list[dict[str, Any]] = []
        compare_rows: list[dict[str, Any]] = []
        offered_model_used: str | None = None

        offered_rows_indexed: list[dict[str, Any]] = []
        if args.requested_vision_only:
            offered_model_used = "db_existing_extraction"
            offered_rows_indexed = _build_offered_rows_from_db(
                db=db,
                campaign_id=campaign_id,
                max_invoices=max(1, int(args.max_invoices)),
            )
        else:
            invoice_rows = (
                db.query(InboundInvoice)
                .filter(InboundInvoice.campaign_id == campaign_id)
                .order_by(InboundInvoice.id.asc())
                .limit(max(1, int(args.max_invoices)))
                .all()
            )
            for inv in invoice_rows:
                file_path = str(getattr(inv, "file_path", "") or "").strip()
                if not file_path or not Path(file_path).exists():
                    continue
                images = _extract_input_images(file_path, max_pages=max(1, int(args.max_pages)))
                if not images:
                    continue
                offered_rows, used_model = _call_gpt4_vision_offered_rows(
                    image_inputs=images,
                    model=model,
                    fallback_model=fallback,
                    invoice_id=int(inv.id),
                )
                if used_model:
                    offered_model_used = used_model
                for idx, off in enumerate(offered_rows, start=1):
                    offered_rows_indexed.append(
                        {
                            "invoice_id": int(inv.id),
                            "invoice_file": str(getattr(inv, "file_name", "") or "").strip(),
                            "row_idx": idx,
                            **off,
                        }
                    )

        if not offered_rows_indexed:
            print("NO_OFFERED_ROWS")
            return 1

        for off in offered_rows_indexed:
            inv_id = int(off.get("invoice_id") or 0)
            idx = int(off.get("row_idx") or 0)
            inv_file = str(off.get("invoice_file") or "").strip()
            if not off.get("name"):
                continue
                strict_payload = {
                    "name": off.get("name"),
                    "quantity": off.get("quantity"),
                    "unit_price": off.get("unit_price"),
                    "line_total": off.get("line_total"),
                    "unit": off.get("unit"),
                    "packaging": off.get("packaging"),
                    "dimensions": off.get("dimensions"),
                    "size": off.get("size"),
                    "weight": off.get("weight"),
                    "vat_rate": off.get("vat_rate"),
                    "confidence": off.get("confidence"),
                    "unit_missing": not bool(str(off.get("unit") or "").strip()),
                    "unit_inferred": None,
                }
                offered_vision_rows.append(
                    {
                        "invoice_id": inv_id,
                        "invoice_file": inv_file,
                        "row_idx": idx,
                        **off,
                    }
                )

                temp_item = SimpleNamespace(
                    id=int(inv.id) * 100000 + int(idx),
                    strict_components_payload=strict_payload,
                    corrected_product_name=None,
                    product_name=str(off.get("name") or "").strip(),
                )
                diagnostics: dict[str, Any] = {}
                matched = _pick_requested_reference(
                    item=temp_item,
                    matched_name=None,
                    candidates=requested_candidates,
                    diagnostics=diagnostics,
                )
                if not isinstance(matched, dict):
                    if args.only_matched:
                        continue
                    compare_rows.append(
                        {
                            "invoice_id": inv_id,
                            "invoice_file": inv_file,
                            "offered_row_idx": idx,
                            "requested_product_id": None,
                            "requested_name": None,
                            "offered_name": off.get("name"),
                            "strict_match_score": diagnostics.get("confidence"),
                            "strict_match_threshold": diagnostics.get("threshold"),
                            "strict_match_decision": diagnostics.get("decision"),
                            "strict_match_reason": diagnostics.get("reason"),
                            "match_payload": None,
                        }
                    )
                    continue

                requested_line_text = _requested_line_text_from_row(matched)
                unit_payload = _build_match_unit_payload(
                    strict_payload=strict_payload,
                    requested_product_name=str(matched.get("product_name") or ""),
                    requested_description=str(matched.get("requested_description") or ""),
                    requested_characteristics=matched.get("requested_characteristics"),
                    requested_line_text=requested_line_text,
                )
                match_payload = _build_match_comparison_payload(
                    eq_payload={},
                    strict_payload=strict_payload,
                    unit_payload=unit_payload,
                    requested_product_name=str(matched.get("product_name") or ""),
                    requested_description=str(matched.get("requested_description") or ""),
                    requested_characteristics=matched.get("requested_characteristics"),
                    requested_line_text=requested_line_text,
                    strict_match_score=_to_float(diagnostics.get("confidence")),
                    strict_match_threshold=_to_float(diagnostics.get("threshold")),
                    strict_match_decision=str(diagnostics.get("decision") or "") or None,
                    strict_mode=True,
                )
                compare_rows.append(
                    {
                        "invoice_id": inv_id,
                        "invoice_file": inv_file,
                        "offered_row_idx": idx,
                        "requested_product_id": matched.get("product_id"),
                        "requested_name": matched.get("product_name"),
                        "offered_name": off.get("name"),
                        "strict_match_score": diagnostics.get("confidence"),
                        "strict_match_threshold": diagnostics.get("threshold"),
                        "strict_match_decision": diagnostics.get("decision"),
                        "strict_match_reason": diagnostics.get("reason"),
                        **match_payload,
                    }
                )

        print(f"MODEL_REQUESTED={model}")
        print(f"MODEL_USED_REQUESTED={requested_model_used}")
        print(f"MODEL_USED_OFFERED={offered_model_used}")
        print(f"REQUESTED_ROWS={len(requested_vision_rows)}")
        print(f"OFFERED_ROWS={len(offered_vision_rows)}")
        print(f"COMPARE_ROWS={len(compare_rows)}")
        matched_rows = [r for r in compare_rows if str(r.get("strict_match_decision") or "").strip().lower() == "matched"]
        print(f"MATCHED_ROWS={len(matched_rows)}")
        print("-" * 260)
        print(
            "inv_id | idx | requested | offered | strict_score | strict_decision | mode | compensable | "
            "factor | basis | unit_mode | unit_compatible | unit_inferred | badges"
        )
        print("-" * 260)
        for row in compare_rows:
            print(
                f"{row.get('invoice_id')} | {row.get('offered_row_idx')} | {row.get('requested_name')} | {row.get('offered_name')} | "
                f"{row.get('strict_match_score')} | {row.get('strict_match_decision')} | {row.get('match_compensation_mode')} | "
                f"{row.get('match_compensable')} | {row.get('match_compensation_factor')} | {row.get('match_compensation_basis')} | "
                f"{row.get('match_unit_compare_mode')} | {row.get('match_unit_compatible')} | {row.get('match_unit_inferred')} | "
                f"{row.get('match_badges')}"
            )

        out_payload = {
            "campaign_id": campaign_id,
            "model_requested": model,
            "model_used_requested": requested_model_used,
            "model_used_offered": offered_model_used,
            "requested_vision_rows": requested_vision_rows,
            "offered_vision_rows": offered_vision_rows,
            "comparison_rows": compare_rows,
        }
        out_path = str(args.out or "").strip() or f"/tmp/compare_requested_offered_gpt4_vision_campaign_{campaign_id}.json"
        Path(out_path).write_text(json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("-" * 260)
        print(f"JSON_SAVED={out_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
