from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from app import config
from app.db import SessionLocal
from app.models import InboundInvoice, InvoiceExtractionItem
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


def _build_parser_rows(invoice_id: int, db) -> list[dict[str, Any]]:
    rows = (
        db.query(InvoiceExtractionItem)
        .filter(InvoiceExtractionItem.invoice_id == invoice_id)
        .order_by(InvoiceExtractionItem.id.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for it in rows:
        strict = it.strict_components_payload if isinstance(it.strict_components_payload, dict) else {}
        out.append(
            {
                "item_id": int(it.id),
                "name": str(it.product_name or "").strip(),
                "quantity": _to_float(it.corrected_quantity if it.corrected_quantity is not None else it.quantity),
                "unit_price": _to_float(it.corrected_unit_price if it.corrected_unit_price is not None else it.unit_price),
                "line_total": _to_float(strict.get("line_total")),
                "unit": str(strict.get("unit") or "").strip() or None,
                "packaging": str(strict.get("packaging") or "").strip() or None,
                "weight": str(strict.get("weight") or "").strip() or None,
                "dimensions": str(strict.get("dimensions") or "").strip() or None,
            }
        )
    return out


def _call_gpt4_vision_rows(
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
                    "notes": "Use images only.",
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
        operation="invoice_strict_vision_test",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=max(30, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45)),
        retry_attempts=max(1, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(0.2, float(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0) or 1.0)),
        metadata={"invoice_id": invoice_id, "images": len(image_inputs)},
    )
    rows = parsed.get("rows") if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list) else []
    clean_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean_rows.append(
            {
                "name": str(row.get("name") or "").strip(),
                "quantity": _to_float(row.get("quantity")),
                "unit_price": _to_float(row.get("unit_price")),
                "line_total": _to_float(row.get("line_total")),
                "unit": str(row.get("unit") or "").strip() or None,
                "packaging": str(row.get("packaging") or "").strip() or None,
                "weight": str(row.get("weight") or "").strip() or None,
                "dimensions": str(row.get("dimensions") or "").strip() or None,
                "vat_rate": _to_float(row.get("vat_rate")),
                "confidence": _to_float(row.get("confidence")),
            }
        )
    return clean_rows, used_model


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test strict extraction with GPT-4 vision (no prod impact)."
    )
    parser.add_argument("--invoice-id", type=int, required=True)
    parser.add_argument("--model", type=str, default="gpt-4o")
    parser.add_argument("--fallback-model", type=str, default="")
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY missing")
        return 1

    db = SessionLocal()
    try:
        invoice = db.query(InboundInvoice).filter(InboundInvoice.id == int(args.invoice_id)).first()
        if not invoice:
            print("INVOICE_NOT_FOUND")
            return 1
        file_path = str(getattr(invoice, "file_path", "") or "").strip()
        if not file_path:
            print("INVOICE_FILE_PATH_MISSING")
            return 1
        if not Path(file_path).exists():
            print(f"FILE_NOT_FOUND: {file_path}")
            return 1

        parser_rows = _build_parser_rows(int(args.invoice_id), db)
        images = _extract_input_images(file_path, max_pages=max(1, int(args.max_pages)))
        if not images:
            print("NO_IMAGES_EXTRACTED_FROM_FILE")
            print("Tip: test works best on image files or scanned PDFs with embedded images.")
            return 1

        fallback = str(args.fallback_model or "").strip() or None
        vision_rows, used_model = _call_gpt4_vision_rows(
            image_inputs=images,
            model=str(args.model or "gpt-4o").strip() or "gpt-4o",
            fallback_model=fallback,
            invoice_id=int(args.invoice_id),
        )

        print(f"MODEL_USED={used_model}")
        print(f"INVOICE_FILE={file_path}")
        print(f"IMAGES_SENT={len(images)}")
        print(f"PARSER_ROWS={len(parser_rows)}")
        print(f"VISION_ROWS={len(vision_rows)}")
        print("-" * 200)
        print(
            "IDX | PARSER_NAME | PARSER_QTY | PARSER_PU | PARSER_TOTAL || "
            "VISION_NAME | VISION_QTY | VISION_PU | VISION_TOTAL | VISION_UNIT | VISION_PACK | VISION_WEIGHT | VISION_DIM"
        )
        print("-" * 200)
        mx = max(len(parser_rows), len(vision_rows))
        for idx in range(mx):
            p = parser_rows[idx] if idx < len(parser_rows) else {}
            v = vision_rows[idx] if idx < len(vision_rows) else {}
            print(
                f"{idx+1} | {p.get('name')} | {p.get('quantity')} | {p.get('unit_price')} | {p.get('line_total')} || "
                f"{v.get('name')} | {v.get('quantity')} | {v.get('unit_price')} | {v.get('line_total')} | "
                f"{v.get('unit')} | {v.get('packaging')} | {v.get('weight')} | {v.get('dimensions')}"
            )

        out = {
            "invoice_id": int(args.invoice_id),
            "invoice_file_path": file_path,
            "model_requested": str(args.model or "gpt-4o"),
            "model_used": used_model,
            "images_sent": len(images),
            "parser_rows": parser_rows,
            "gpt4_vision_rows": vision_rows,
        }
        out_path = str(args.out or "").strip() or f"/tmp/invoice_{int(args.invoice_id)}_strict_gpt4_vision_test.json"
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("-" * 200)
        print(f"JSON_SAVED={out_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

