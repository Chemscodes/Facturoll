from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import sys
from pathlib import Path
from typing import Any

from pypdf import PdfReader


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import config  # noqa: E402
from app.services.openai_chat import call_chat_completion_json_with_retry  # noqa: E402


SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}


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


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


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


def _call_requested_vision(
    *,
    image_inputs: list[tuple[str, bytes]],
    model: str,
    fallback_model: str | None,
    source_name: str,
) -> tuple[dict[str, Any], str | None]:
    system_prompt = (
        "Extract strict requested product rows from the provided tender images only. "
        "Return explicit products only, no invention. "
        "If a field is unclear, return null. "
        "Return JSON only."
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
                    "task": "strict_requested_products_extraction_from_folder_images",
                    "source_name": source_name,
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
                "name": "requested_products_folder_vision",
                "strict": True,
                "schema": schema,
            },
        },
    }

    parsed, used_model = call_chat_completion_json_with_retry(
        operation="folder_requested_strict_vision",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=max(30, int(getattr(config, "OPENAI_ATTACHMENT_TIMEOUT_SECONDS", 120) or 120)),
        retry_attempts=max(1, int(getattr(config, "OPENAI_ATTACHMENT_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(0.2, float(getattr(config, "OPENAI_ATTACHMENT_RETRY_BACKOFF_SECONDS", 1.0) or 1.0)),
        metadata={"source_name": source_name, "images": len(image_inputs)},
    )
    if not isinstance(parsed, dict):
        return {"rows": [], "unassigned": []}, used_model

    raw_products = parsed.get("products") if isinstance(parsed.get("products"), list) else []
    rows: list[dict[str, Any]] = []
    for row in raw_products:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        rows.append(
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
    return {"rows": rows, "unassigned": unassigned}, used_model


def _call_invoice_vision(
    *,
    image_inputs: list[tuple[str, bytes]],
    model: str,
    fallback_model: str | None,
    source_name: str,
) -> tuple[dict[str, Any], str | None]:
    system_prompt = (
        "Extract strict invoice line items from the provided invoice images only. "
        "Ignore headers, totals, payment lines and footer noise. "
        "Keep the line order. "
        "If a field is unclear, return null. "
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
                    "task": "strict_invoice_extraction_from_folder_images",
                    "source_name": source_name,
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
                "name": "invoice_rows_folder_vision",
                "strict": True,
                "schema": schema,
            },
        },
    }

    parsed, used_model = call_chat_completion_json_with_retry(
        operation="folder_invoice_strict_vision",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=max(30, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45)),
        retry_attempts=max(1, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(0.2, float(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0) or 1.0)),
        metadata={"source_name": source_name, "images": len(image_inputs)},
    )
    rows = parsed.get("rows") if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list) else []
    clean_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        clean_rows.append(
            {
                "name": name,
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
    return {"rows": clean_rows, "unassigned": []}, used_model


def _call_tender_notice_vision(
    *,
    image_inputs: list[tuple[str, bytes]],
    model: str,
    fallback_model: str | None,
    source_name: str,
) -> tuple[dict[str, Any], str | None]:
    system_prompt = (
        "Analyse un avis d'appel d'offres ou consultation depuis les images fournies uniquement. "
        "Extrais les conditions globales et les conditions par lot. "
        "Ne deduis pas agressivement. Si une information est absente ou ambiguë, retourne null. "
        "Identifie si le document reste dans les secteurs medical, paramedical ou pharmaceutique. "
        "Retourne uniquement un JSON strict."
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document_type": {"type": ["string", "null"]},
            "establishment_name": {"type": ["string", "null"]},
            "establishment_phone": {"type": ["string", "null"]},
            "establishment_email": {"type": ["string", "null"]},
            "sectors_detected": {
                "type": "array",
                "items": {"type": "string"},
            },
            "sector_medical_related": {"type": ["boolean", "null"]},
            "global_conditions": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "manufacturer_importer_only": {"type": ["boolean", "null"]},
                    "financial_capacity_max_dzd": {"type": ["number", "null"]},
                    "turnover_max_dzd": {"type": ["number", "null"]},
                    "requires_huissier_pv_for_after_sales_service": {"type": ["boolean", "null"]},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "manufacturer_importer_only",
                    "financial_capacity_max_dzd",
                    "turnover_max_dzd",
                    "requires_huissier_pv_for_after_sales_service",
                    "evidence",
                ],
            },
            "lots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "lot_label": {"type": "string"},
                        "lot_title": {"type": ["string", "null"]},
                        "manufacturer_importer_only": {"type": ["boolean", "null"]},
                        "financial_capacity_max_dzd": {"type": ["number", "null"]},
                        "turnover_max_dzd": {"type": ["number", "null"]},
                        "requires_huissier_pv_for_after_sales_service": {"type": ["boolean", "null"]},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "lot_label",
                        "lot_title",
                        "manufacturer_importer_only",
                        "financial_capacity_max_dzd",
                        "turnover_max_dzd",
                        "requires_huissier_pv_for_after_sales_service",
                        "evidence",
                    ],
                },
            },
            "unassigned_conditions": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "document_type",
            "establishment_name",
            "establishment_phone",
            "establishment_email",
            "sectors_detected",
            "sector_medical_related",
            "global_conditions",
            "lots",
            "unassigned_conditions",
        ],
    }

    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "task": "tender_notice_compliance_extraction_from_folder_images",
                    "source_name": source_name,
                    "target_sector_family": ["medical", "paramedical", "pharmaceutique"],
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
                "name": "tender_notice_compliance_folder_vision",
                "strict": True,
                "schema": schema,
            },
        },
    }

    parsed, used_model = call_chat_completion_json_with_retry(
        operation="folder_tender_notice_strict_vision",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=max(30, int(getattr(config, "OPENAI_ATTACHMENT_TIMEOUT_SECONDS", 120) or 120)),
        retry_attempts=max(1, int(getattr(config, "OPENAI_ATTACHMENT_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(0.2, float(getattr(config, "OPENAI_ATTACHMENT_RETRY_BACKOFF_SECONDS", 1.0) or 1.0)),
        metadata={"source_name": source_name, "images": len(image_inputs)},
    )
    if not isinstance(parsed, dict):
        return {
            "document_type": None,
            "establishment_name": None,
            "establishment_phone": None,
            "establishment_email": None,
            "sectors_detected": [],
            "sector_medical_related": None,
            "global_conditions": {},
            "lots": [],
            "unassigned_conditions": [],
        }, used_model

    global_conditions = parsed.get("global_conditions") if isinstance(parsed.get("global_conditions"), dict) else {}
    lots = parsed.get("lots") if isinstance(parsed.get("lots"), list) else []
    normalized_lots: list[dict[str, Any]] = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        label = str(lot.get("lot_label") or "").strip()
        if not label:
            continue
        normalized_lots.append(
            {
                "lot_label": label,
                "lot_title": str(lot.get("lot_title") or "").strip() or None,
                "manufacturer_importer_only": lot.get("manufacturer_importer_only"),
                "financial_capacity_max_dzd": _to_float(lot.get("financial_capacity_max_dzd")),
                "turnover_max_dzd": _to_float(lot.get("turnover_max_dzd")),
                "requires_huissier_pv_for_after_sales_service": lot.get(
                    "requires_huissier_pv_for_after_sales_service"
                ),
                "evidence": [str(v).strip() for v in lot.get("evidence") or [] if str(v).strip()],
            }
        )

    return {
        "document_type": str(parsed.get("document_type") or "").strip() or None,
        "establishment_name": str(parsed.get("establishment_name") or "").strip() or None,
        "establishment_phone": str(parsed.get("establishment_phone") or "").strip() or None,
        "establishment_email": str(parsed.get("establishment_email") or "").strip() or None,
        "sectors_detected": [str(v).strip() for v in parsed.get("sectors_detected") or [] if str(v).strip()],
        "sector_medical_related": parsed.get("sector_medical_related"),
        "global_conditions": {
            "manufacturer_importer_only": global_conditions.get("manufacturer_importer_only"),
            "financial_capacity_max_dzd": _to_float(global_conditions.get("financial_capacity_max_dzd")),
            "turnover_max_dzd": _to_float(global_conditions.get("turnover_max_dzd")),
            "requires_huissier_pv_for_after_sales_service": global_conditions.get(
                "requires_huissier_pv_for_after_sales_service"
            ),
            "evidence": [str(v).strip() for v in global_conditions.get("evidence") or [] if str(v).strip()],
        },
        "lots": normalized_lots,
        "unassigned_conditions": [
            str(v).strip() for v in parsed.get("unassigned_conditions") or [] if str(v).strip()
        ],
    }, used_model


def _row_has_field(row: dict[str, Any], field_name: str) -> bool:
    value = row.get(field_name)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _row_name_matches(row: dict[str, Any], spec: dict[str, Any]) -> bool:
    name = _norm_text(row.get("name"))
    if not name:
        return False
    contains_any = spec.get("name_contains_any")
    if isinstance(contains_any, list):
        tokens = [_norm_text(v) for v in contains_any if _norm_text(v)]
        if tokens and not any(token in name for token in tokens):
            return False
    contains_all = spec.get("name_contains_all")
    if isinstance(contains_all, list):
        tokens = [_norm_text(v) for v in contains_all if _norm_text(v)]
        if tokens and not all(token in name for token in tokens):
            return False
    regex = str(spec.get("name_regex") or "").strip()
    if regex:
        import re

        if re.search(regex, str(row.get("name") or ""), flags=re.IGNORECASE) is None:
            return False
    return True


def _row_matches_spec(row: dict[str, Any], spec: dict[str, Any]) -> bool:
    if not _row_name_matches(row, spec):
        return False
    require_fields = spec.get("require_fields")
    if isinstance(require_fields, list):
        fields = [str(v).strip() for v in require_fields if str(v).strip()]
        if any(not _row_has_field(row, field_name) for field_name in fields):
            return False
    require_numeric = spec.get("require_numeric_fields")
    if isinstance(require_numeric, list):
        fields = [str(v).strip() for v in require_numeric if str(v).strip()]
        if any(_to_float(row.get(field_name)) is None for field_name in fields):
            return False
    min_confidence = _to_float(spec.get("min_confidence"))
    if min_confidence is not None:
        confidence = _to_float(row.get("confidence"))
        if confidence is None or confidence < min_confidence:
            return False
    return True


def _evaluate_conditions(extracted: dict[str, Any], conditions: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    rows = extracted.get("rows") if isinstance(extracted.get("rows"), list) else []
    unassigned = extracted.get("unassigned") if isinstance(extracted.get("unassigned"), list) else []
    reasons: list[str] = []
    stats: dict[str, Any] = {
        "rows": len(rows),
        "unassigned": len(unassigned),
        "matching_rows": 0,
    }

    min_rows = int(conditions.get("min_rows", 0) or 0)
    if len(rows) < min_rows:
        reasons.append(f"rows<{min_rows}")

    max_rows_raw = conditions.get("max_rows")
    if max_rows_raw not in (None, ""):
        max_rows = int(max_rows_raw)
        if len(rows) > max_rows:
            reasons.append(f"rows>{max_rows}")

    max_unassigned_raw = conditions.get("max_unassigned")
    if max_unassigned_raw not in (None, ""):
        max_unassigned = int(max_unassigned_raw)
        if len(unassigned) > max_unassigned:
            reasons.append(f"unassigned>{max_unassigned}")

    min_confidence = _to_float(conditions.get("min_confidence"))
    if min_confidence is not None:
        low_conf = [
            idx + 1
            for idx, row in enumerate(rows)
            if (_to_float(row.get("confidence")) or 0.0) < min_confidence
        ]
        if low_conf:
            reasons.append(f"confidence<{min_confidence}")
            stats["rows_below_min_confidence"] = low_conf

    require_fields_all = conditions.get("require_fields_all_rows")
    if isinstance(require_fields_all, list):
        fields = [str(v).strip() for v in require_fields_all if str(v).strip()]
        missing = []
        for idx, row in enumerate(rows):
            for field_name in fields:
                if not _row_has_field(row, field_name):
                    missing.append({"row": idx + 1, "field": field_name})
        if missing:
            reasons.append("missing_required_fields_all_rows")
            stats["missing_required_fields_all_rows"] = missing

    require_fields_any = conditions.get("require_fields_any_row")
    if isinstance(require_fields_any, list):
        fields = [str(v).strip() for v in require_fields_any if str(v).strip()]
        found = any(all(_row_has_field(row, field_name) for field_name in fields) for row in rows)
        if fields and not found:
            reasons.append("missing_required_fields_any_row")

    require_numeric_all = conditions.get("require_numeric_all_rows")
    if isinstance(require_numeric_all, list):
        fields = [str(v).strip() for v in require_numeric_all if str(v).strip()]
        missing = []
        for idx, row in enumerate(rows):
            for field_name in fields:
                if _to_float(row.get(field_name)) is None:
                    missing.append({"row": idx + 1, "field": field_name})
        if missing:
            reasons.append("missing_required_numeric_all_rows")
            stats["missing_required_numeric_all_rows"] = missing

    require_numeric_any = conditions.get("require_numeric_any_row")
    if isinstance(require_numeric_any, list):
        fields = [str(v).strip() for v in require_numeric_any if str(v).strip()]
        found = any(all(_to_float(row.get(field_name)) is not None for field_name in fields) for row in rows)
        if fields and not found:
            reasons.append("missing_required_numeric_any_row")

    row_match_spec = conditions.get("matching_row")
    if isinstance(row_match_spec, dict):
        matching_rows = [row for row in rows if _row_matches_spec(row, row_match_spec)]
        stats["matching_rows"] = len(matching_rows)
        min_matching_rows = int(conditions.get("min_matching_rows", 1) or 0)
        if len(matching_rows) < min_matching_rows:
            reasons.append(f"matching_rows<{min_matching_rows}")
        max_matching_rows_raw = conditions.get("max_matching_rows")
        if max_matching_rows_raw not in (None, ""):
            max_matching_rows = int(max_matching_rows_raw)
            if len(matching_rows) > max_matching_rows:
                reasons.append(f"matching_rows>{max_matching_rows}")

    keep = not reasons
    return keep, reasons, stats


def _evaluate_tender_notice_conditions(
    extracted: dict[str, Any],
    conditions: dict[str, Any],
) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    sectors = [str(v).strip() for v in extracted.get("sectors_detected") or [] if str(v).strip()]
    lots = extracted.get("lots") if isinstance(extracted.get("lots"), list) else []
    global_conditions = extracted.get("global_conditions") if isinstance(extracted.get("global_conditions"), dict) else {}
    stats: dict[str, Any] = {
        "lots_total": len(lots),
        "eligible_lots": 0,
        "sectors_detected": sectors,
    }

    allowed_sector_tokens = [_norm_text(v) for v in conditions.get("allowed_sector_tokens", []) if _norm_text(v)]
    sector_related = extracted.get("sector_medical_related")
    if sector_related is False:
        reasons.append("sector_not_medical_related")
    elif allowed_sector_tokens:
        sector_text = " ".join(_norm_text(v) for v in sectors)
        if sector_text and not any(token in sector_text for token in allowed_sector_tokens):
            reasons.append("sector_outside_allowed_scope")

    if bool(global_conditions.get("manufacturer_importer_only")):
        reasons.append("global_manufacturer_importer_only")

    financial_cap_limit = _to_float(conditions.get("max_financial_capacity_dzd"))
    turnover_limit = _to_float(conditions.get("max_turnover_dzd"))
    combined_limit = _to_float(conditions.get("max_financial_or_turnover_dzd"))
    if combined_limit is not None:
        if financial_cap_limit is None:
            financial_cap_limit = combined_limit
        if turnover_limit is None:
            turnover_limit = combined_limit

    global_financial = _to_float(global_conditions.get("financial_capacity_max_dzd"))
    global_turnover = _to_float(global_conditions.get("turnover_max_dzd"))
    if financial_cap_limit is not None and global_financial is not None and global_financial > financial_cap_limit:
        reasons.append(f"global_financial_capacity>{int(financial_cap_limit)}")
    if turnover_limit is not None and global_turnover is not None and global_turnover > turnover_limit:
        reasons.append(f"global_turnover>{int(turnover_limit)}")

    if bool(global_conditions.get("requires_huissier_pv_for_after_sales_service")):
        reasons.append("global_requires_huissier_pv_sav")

    eligible_lots: list[str] = []
    rejected_lots: list[dict[str, Any]] = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        lot_reasons: list[str] = []
        if bool(lot.get("manufacturer_importer_only")):
            lot_reasons.append("manufacturer_importer_only")
        lot_financial = _to_float(lot.get("financial_capacity_max_dzd"))
        lot_turnover = _to_float(lot.get("turnover_max_dzd"))
        if financial_cap_limit is not None and lot_financial is not None and lot_financial > financial_cap_limit:
            lot_reasons.append(f"financial_capacity>{int(financial_cap_limit)}")
        if turnover_limit is not None and lot_turnover is not None and lot_turnover > turnover_limit:
            lot_reasons.append(f"turnover>{int(turnover_limit)}")
        if bool(lot.get("requires_huissier_pv_for_after_sales_service")):
            lot_reasons.append("requires_huissier_pv_sav")
        lot_label = str(lot.get("lot_label") or "").strip() or "lot"
        if lot_reasons:
            rejected_lots.append({"lot_label": lot_label, "reasons": lot_reasons})
        else:
            eligible_lots.append(lot_label)

    stats["eligible_lots"] = len(eligible_lots)
    stats["eligible_lot_labels"] = eligible_lots
    stats["rejected_lots"] = rejected_lots
    stats["establishment_name"] = extracted.get("establishment_name")
    stats["establishment_phone"] = extracted.get("establishment_phone")
    stats["establishment_email"] = extracted.get("establishment_email")

    min_eligible_lots = int(conditions.get("min_eligible_lots", 1) or 0)
    if lots and len(eligible_lots) < min_eligible_lots:
        reasons.append(f"eligible_lots<{min_eligible_lots}")

    if conditions.get("require_lots_detected") and not lots:
        reasons.append("no_lots_detected")

    keep = not reasons
    return keep, reasons, stats


def _process_file(
    *,
    path: Path,
    mode: str,
    model: str,
    fallback_model: str | None,
    max_pages: int,
    conditions: dict[str, Any],
) -> dict[str, Any]:
    images = _extract_input_images(str(path), max_pages=max_pages)
    if not images:
        return {
            "file_name": path.name,
            "file_path": str(path),
            "mode": mode,
            "used_model": None,
            "keep": False,
            "reasons": ["no_images_extracted"],
            "stats": {"rows": 0, "unassigned": 0, "matching_rows": 0},
            "rows": [],
            "unassigned": [],
        }

    if mode == "requested":
        extracted, used_model = _call_requested_vision(
            image_inputs=images,
            model=model,
            fallback_model=fallback_model,
            source_name=path.name,
        )
    elif mode == "invoice":
        extracted, used_model = _call_invoice_vision(
            image_inputs=images,
            model=model,
            fallback_model=fallback_model,
            source_name=path.name,
        )
    else:
        extracted, used_model = _call_tender_notice_vision(
            image_inputs=images,
            model=model,
            fallback_model=fallback_model,
            source_name=path.name,
        )

    if mode == "tender_notice":
        keep, reasons, stats = _evaluate_tender_notice_conditions(extracted, conditions)
    else:
        keep, reasons, stats = _evaluate_conditions(extracted, conditions)
    return {
        "file_name": path.name,
        "file_path": str(path),
        "mode": mode,
        "used_model": used_model,
        "keep": keep,
        "reasons": reasons,
        "stats": stats,
        "rows": extracted.get("rows") if isinstance(extracted.get("rows"), list) else [],
        "unassigned": extracted.get("unassigned") if isinstance(extracted.get("unassigned"), list) else [],
        "tender_notice": extracted if mode == "tender_notice" else None,
    }


def _iter_input_files(folder: Path) -> list[Path]:
    files: list[Path] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        files.append(path)
    files.sort(key=lambda p: str(p).lower())
    return files


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Process a folder of images/PDFs with GPT vision, then keep only files whose "
            "extracted JSON matches the required conditions."
        )
    )
    parser.add_argument("--input-dir", required=True, type=str)
    parser.add_argument("--output-dir", required=True, type=str)
    parser.add_argument("--mode", choices=["requested", "invoice", "tender_notice"], default="invoice")
    parser.add_argument("--conditions-file", required=True, type=str)
    parser.add_argument("--model", type=str, default="gpt-4o")
    parser.add_argument("--fallback-model", type=str, default="")
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--copy-rejected", action="store_true")
    args = parser.parse_args()

    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY missing")
        return 1

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    conditions_path = Path(args.conditions_file).resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"INPUT_DIR_NOT_FOUND: {input_dir}")
        return 1
    if not conditions_path.exists():
        print(f"CONDITIONS_FILE_NOT_FOUND: {conditions_path}")
        return 1

    conditions = json.loads(conditions_path.read_text(encoding="utf-8"))
    files = _iter_input_files(input_dir)
    if not files:
        print("NO_SUPPORTED_FILES_FOUND")
        return 1

    kept_dir = output_dir / "kept"
    rejected_dir = output_dir / "rejected"
    json_dir = output_dir / "json"
    kept_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    if args.copy_rejected:
        rejected_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for idx, path in enumerate(files, start=1):
        result = _process_file(
            path=path,
            mode=args.mode,
            model=str(args.model or "").strip() or "gpt-4o",
            fallback_model=str(args.fallback_model or "").strip() or None,
            max_pages=max(1, int(args.max_pages)),
            conditions=conditions,
        )
        results.append(result)
        stem = Path(result["file_name"]).stem
        (json_dir / f"{stem}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        destination_dir = kept_dir if result["keep"] else rejected_dir
        if result["keep"] or args.copy_rejected:
            shutil.copy2(path, destination_dir / path.name)
        if args.mode == "tender_notice":
            eligible_lots = result["stats"].get("eligible_lot_labels") or []
            establishment_name = result["stats"].get("establishment_name") or "-"
            print(
                f"[{idx}/{len(files)}] {path.name} -> "
                f"{'KEEP' if result['keep'] else 'REJECT'} "
                f"eligible_lots={len(eligible_lots)} "
                f"establishment={establishment_name} "
                f"lots={'; '.join(eligible_lots) if eligible_lots else '-'} "
                f"reasons={','.join(result['reasons']) if result['reasons'] else '-'}"
            )
        else:
            print(
                f"[{idx}/{len(files)}] {path.name} -> "
                f"{'KEEP' if result['keep'] else 'REJECT'} "
                f"rows={result['stats'].get('rows', 0)} "
                f"reasons={','.join(result['reasons']) if result['reasons'] else '-'}"
            )

    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "mode": args.mode,
        "model": str(args.model or "").strip() or "gpt-4o",
        "files_total": len(results),
        "files_kept": sum(1 for row in results if row.get("keep")),
        "files_rejected": sum(1 for row in results if not row.get("keep")),
        "conditions": conditions,
        "results": results,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SUMMARY_SAVED={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
