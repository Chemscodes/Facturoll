from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from app import config
from app.db import SessionLocal
from app.models import CampaignAttachment
from app.services.openai_chat import call_chat_completion_json_with_retry


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


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


def _call_gpt4_vision_strict_products(
    *,
    image_inputs: list[tuple[str, bytes]],
    model: str,
    fallback_model: str | None,
    attachment_id: int,
    file_name: str,
) -> tuple[dict[str, Any], str | None]:
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
            },
            "unassigned": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["products", "unassigned"],
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
        operation="requested_products_strict_vision_test",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=max(30, int(getattr(config, "OPENAI_ATTACHMENT_TIMEOUT_SECONDS", 120) or 120)),
        retry_attempts=max(1, int(getattr(config, "OPENAI_ATTACHMENT_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(0.2, float(getattr(config, "OPENAI_ATTACHMENT_RETRY_BACKOFF_SECONDS", 1.0) or 1.0)),
        metadata={"attachment_id": attachment_id, "images": len(image_inputs)},
    )
    if not isinstance(parsed, dict):
        return {"products": [], "unassigned": []}, used_model

    raw_products = parsed.get("products") if isinstance(parsed.get("products"), list) else []
    products: list[dict[str, Any]] = []
    for row in raw_products:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        products.append(
            {
                "name": name,
                "quantity": _to_float(row.get("quantity")),
                "unit": str(row.get("unit") or "").strip() or None,
                "packaging": str(row.get("packaging") or "").strip() or None,
                "dimensions": str(row.get("dimensions") or "").strip() or None,
                "size": str(row.get("size") or "").strip() or None,
                "weight": str(row.get("weight") or "").strip() or None,
                "vat_rate": _to_float(row.get("vat_rate")),
                "confidence": _to_float(row.get("confidence")),
            }
        )
    unassigned = []
    raw_unassigned = parsed.get("unassigned")
    if isinstance(raw_unassigned, list):
        for value in raw_unassigned:
            text = str(value or "").strip()
            if text:
                unassigned.append(text)

    return {"products": products, "unassigned": unassigned}, used_model


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test strict requested extraction with GPT-4 vision on campaign attachment."
    )
    parser.add_argument("--attachment-id", type=int, required=True)
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
        attachment = (
            db.query(CampaignAttachment)
            .filter(CampaignAttachment.id == int(args.attachment_id))
            .first()
        )
        if not attachment:
            print("ATTACHMENT_NOT_FOUND")
            return 1

        file_path = str(getattr(attachment, "file_path", "") or "").strip()
        file_name = str(getattr(attachment, "file_name", "") or "").strip()
        if not file_path:
            print("ATTACHMENT_FILE_PATH_MISSING")
            return 1
        if not Path(file_path).exists():
            print(f"FILE_NOT_FOUND: {file_path}")
            return 1

        images = _extract_input_images(file_path, max_pages=max(1, int(args.max_pages)))
        if not images:
            print("NO_IMAGES_EXTRACTED_FROM_FILE")
            print("Tip: vision test works best on scanned PDF/image attachments.")
            return 1

        fallback = str(args.fallback_model or "").strip() or None
        extracted, used_model = _call_gpt4_vision_strict_products(
            image_inputs=images,
            model=str(args.model or "gpt-4o").strip() or "gpt-4o",
            fallback_model=fallback,
            attachment_id=int(args.attachment_id),
            file_name=file_name,
        )

        stored_preview = (
            attachment.strict_extraction_preview
            if isinstance(attachment.strict_extraction_preview, dict)
            else {}
        )
        stored_products = (
            stored_preview.get("products")
            if isinstance(stored_preview.get("products"), list)
            else []
        )
        new_products = extracted.get("products") if isinstance(extracted.get("products"), list) else []

        # Comparison summary by index (quick operational check).
        exact_name_matches = 0
        near_name_matches = 0
        qty_matches = 0
        price_like_qty_conflicts = 0
        compared = max(len(stored_products), len(new_products))
        diffs: list[dict[str, Any]] = []
        for idx in range(compared):
            old_row = stored_products[idx] if idx < len(stored_products) and isinstance(stored_products[idx], dict) else {}
            new_row = new_products[idx] if idx < len(new_products) and isinstance(new_products[idx], dict) else {}
            old_name = str(old_row.get("name") or "").strip()
            new_name = str(new_row.get("name") or "").strip()
            old_norm = _norm_text(old_name)
            new_norm = _norm_text(new_name)
            if old_norm and new_norm and old_norm == new_norm:
                exact_name_matches += 1
            elif old_norm and new_norm:
                import difflib

                ratio = difflib.SequenceMatcher(None, old_norm, new_norm).ratio()
                if ratio >= 0.85:
                    near_name_matches += 1
            old_qty = _to_float(old_row.get("quantity"))
            new_qty = _to_float(new_row.get("quantity"))
            if old_qty is not None and new_qty is not None and abs(old_qty - new_qty) <= 1e-9:
                qty_matches += 1
            if new_qty is not None and new_qty > 100000:
                price_like_qty_conflicts += 1

            if old_norm != new_norm or old_qty != new_qty:
                diffs.append(
                    {
                        "idx": idx + 1,
                        "stored_name": old_name or None,
                        "vision_name": new_name or None,
                        "stored_qty": old_qty,
                        "vision_qty": new_qty,
                    }
                )

        print(f"MODEL_USED={used_model}")
        print(f"ATTACHMENT_ID={attachment.id}")
        print(f"CAMPAIGN_ID={attachment.campaign_id}")
        print(f"FILE={file_path}")
        print(f"IMAGES_SENT={len(images)}")
        print(f"STORED_STRICT_ROWS={len(stored_products)}")
        print(f"GPT4_VISION_ROWS={len(new_products)}")
        print(
            "COMPARE_SUMMARY="
            + json.dumps(
                {
                    "compared_rows": compared,
                    "exact_name_matches": exact_name_matches,
                    "near_name_matches": near_name_matches,
                    "qty_exact_matches": qty_matches,
                    "vision_qty_outlier_gt_100000": price_like_qty_conflicts,
                    "diff_rows": len(diffs),
                },
                ensure_ascii=False,
            )
        )
        print("-" * 180)
        print("IDX | STORED_NAME || GPT4_VISION_NAME")
        print("-" * 180)
        mx = max(len(stored_products), len(new_products))
        for idx in range(mx):
            left = (
                str(stored_products[idx].get("name") or "").strip()
                if idx < len(stored_products) and isinstance(stored_products[idx], dict)
                else None
            )
            right = (
                str(new_products[idx].get("name") or "").strip()
                if idx < len(new_products) and isinstance(new_products[idx], dict)
                else None
            )
            print(f"{idx+1} | {left} || {right}")

        out_payload = {
            "attachment_id": int(attachment.id),
            "campaign_id": int(attachment.campaign_id),
            "file_name": file_name,
            "file_path": file_path,
            "model_requested": str(args.model or "gpt-4o"),
            "model_used": used_model,
            "images_sent": len(images),
            "compare_summary": {
                "compared_rows": compared,
                "exact_name_matches": exact_name_matches,
                "near_name_matches": near_name_matches,
                "qty_exact_matches": qty_matches,
                "vision_qty_outlier_gt_100000": price_like_qty_conflicts,
                "diff_rows": len(diffs),
            },
            "diff_rows": diffs,
            "stored_strict_preview": stored_preview,
            "gpt4_vision_strict_preview": extracted,
        }
        out_path = str(args.out or "").strip() or f"/tmp/requested_attachment_{int(attachment.id)}_gpt4_vision_test.json"
        Path(out_path).write_text(json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("-" * 180)
        print(f"JSON_SAVED={out_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
