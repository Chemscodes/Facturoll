import re
import logging
import unicodedata
import difflib
import math
from typing import Any
import json
import hashlib
import base64
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import config
from app.auth import get_current_user
from app.db import get_db
from app.models import (
    Campaign,
    CampaignItem,
    CampaignAttachment,
    InvoiceExtractionItem,
    InvoiceItemCharacteristicMatch,
    InvoiceItemOfferAnalysisCache,
    OfferAnalysisResult,
    ExtractionStatus,
    InboundInvoice,
    TechSheet,
    TechSheetLink,
    Product,
    User,
)
from app.schemas import (
    InvoiceExtractionItemOut,
    InvoiceExtractionItemUpdate,
    InvoiceExtractionItemCreate,
    TechSheetLinkPayload,
)
from app.services.invoice_extraction import (
    _collect_campaign_requested_characteristics,
    _compare_characteristics_with_embeddings,
    _extract_offered_characteristics,
    _extract_pdf_text,
    _select_requested_characteristics_for_product,
    apply_invoice_fallback_for_low_gpt_score,
    rebuild_characteristics_pair_cache,
    extract_line_items,
    match_products_hybrid,
    resolve_invoice_processing_file_path,
)

_CAMPAIGN_PROCESSING_STANDARD = "standard"
_CAMPAIGN_PROCESSING_STRICT = "strict"


def _normalize_campaign_processing_mode(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"strict", "strict_mode", "mode_strict"}:
        return _CAMPAIGN_PROCESSING_STRICT
    if text in {"standard", "std", "normal", "classic", "non_strict", "non-strict"}:
        return _CAMPAIGN_PROCESSING_STANDARD
    return None
from app.services.openai_payload_cache import (
    get_openai_payload_cache,
    set_openai_payload_cache,
)
from app.services.openai_chat import call_chat_completion_json_with_retry
from app.settings_service import get_settings, get_user_embedding_thresholds

router = APIRouter(prefix="/invoices", tags=["invoice-extractions"])
logger = logging.getLogger("app")


def _auto_link_tech_sheets(
    db: Session,
    current_user: User,
    campaign_id: int,
    *,
    invoice_id: int | None = None,
    sender_email: str | None = None,
) -> None:
    try:
        # Local import to avoid hard coupling at module import time.
        from app.routes.tech_sheets import link_tech_sheets

        link_tech_sheets(
            TechSheetLinkPayload(
                campaign_id=campaign_id,
                invoice_id=invoice_id,
                sender_email=sender_email,
                min_score=0.60,
                max_text_chars=2000,
            ),
            db=db,
            current_user=current_user,
        )
    except Exception as exc:
        logger.warning(
            "Auto tech-sheet link failed: tenant=%s campaign=%s invoice=%s error=%s",
            current_user.tenant_id,
            campaign_id,
            invoice_id,
            exc,
        )


def _try_warm_results_cache(
    db: Session,
    current_user: User,
    campaign_id: int | None,
) -> None:
    if campaign_id is None:
        return
    try:
        from app.routes.campaigns import warm_results_cache_for_campaign

        warm_results_cache_for_campaign(
            campaign_id=campaign_id,
            db=db,
            current_user=current_user,
        )
    except Exception as exc:
        logger.warning(
            "Results cache warm failed: tenant=%s user=%s campaign=%s err=%s",
            current_user.tenant_id,
            current_user.id,
            campaign_id,
            exc,
        )


def _clean_display_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\u00A0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return text
    if any(h in text for h in ("Ãƒ", "Ã‚", "Ã¢", "Ã", "Ã‘")):
        for enc in ("latin1", "cp1252"):
            try:
                repaired = text.encode(enc).decode("utf-8")
            except Exception:
                continue
            if repaired and repaired.count("\uFFFD") <= text.count("\uFFFD"):
                text = re.sub(r"\s+", " ", repaired).strip()
                break
    return text


def _parse_score(notes: str | None) -> float | None:
    if not notes:
        return None
    match = re.search(r"score=([0-9.]+)", notes)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _norm(text: str | None) -> str:
    return " ".join((text or "").strip().lower().split())


def _extract_emb_score_from_notes(notes: str | None) -> float | None:
    if not notes:
        return None
    match = re.search(r"\bemb_score=([0-9]+(?:\.[0-9]+)?)", notes)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_gpt_score_from_notes(notes: str | None) -> float | None:
    if not notes:
        return None
    match = re.search(r"\bgpt_score=([0-9]+(?:\.[0-9]+)?)", notes)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_match_name_from_notes(notes: str | None) -> str | None:
    if not notes:
        return None
    patterns = [
        r"\bgpt_match=(.+?)(?:\s+gpt_score=|$)",
        r"\bemb_match=(.+?)(?:\s+emb_score=|$)",
        r"\bfuzzy_match=(.+?)(?:\s+fuzzy_score=|$)",
        r"\bchar_match=(.+?)(?:\s+score=|$)",
        r"\bmatch=(.+?)(?:\s+score=|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, notes)
        if match:
            value = (match.group(1) or "").strip()
            if value:
                return value
    return None


_NAME_STOPWORDS = {
    "de",
    "du",
    "des",
    "ou",
    "plus",
    "pour",
    "avec",
    "une",
    "un",
    "et",
    "la",
    "le",
    "les",
    "a",
    "au",
    "aux",
    "en",
    "d",
}

_TOKEN_SYNONYMS = {
    "sterilisateur": "autoclave",
    "sterilizer": "autoclave",
    "autoclave": "autoclave",
    "benchtop": "paillasse",
    "tabletop": "paillasse",
    "bench": "paillasse",
    "paillasse": "paillasse",
    "trolley": "chariot",
    "cart": "chariot",
    "charriot": "chariot",
    "stand": "support",
    "holder": "support",
    "rack": "panier",
    "tray": "panier",
}

_UNIT_ALIASES = {
    "l": "l",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",
    "ml": "ml",
    "cl": "cl",
    "g": "g",
    "gr": "g",
    "gram": "g",
    "grams": "g",
    "gramme": "g",
    "grammes": "g",
    "kg": "kg",
    "mg": "mg",
    "m": "m",
    "cm": "cm",
    "mm": "mm",
}

_ALLOWED_MEASURE_UNITS = {
    "mm",
    "cm",
    "m",
    "mg",
    "g",
    "kg",
    "ml",
    "cl",
    "l",
}

_UNIT_TO_BASE = {
    "ml": ("l", 0.001),
    "cl": ("l", 0.01),
    "l": ("l", 1.0),
    "mg": ("g", 0.001),
    "g": ("g", 1.0),
    "kg": ("g", 1000.0),
    "mm": ("m", 0.001),
    "cm": ("m", 0.01),
    "m": ("m", 1.0),
}

# Dedicated conversion base for strict offered/requested unit comparison.
# Keeps equivalence explicit as requested by business logic:
# 1 kg = 1000 g, 1 l = 1000 ml, 1 m = 100 cm.
_UNIT_TO_COMPARE_BASE = {
    "ml": ("ml", 1.0),
    "cl": ("ml", 10.0),
    "l": ("ml", 1000.0),
    "mg": ("g", 0.001),
    "g": ("g", 1.0),
    "kg": ("g", 1000.0),
    "mm": ("cm", 0.1),
    "cm": ("cm", 1.0),
    "m": ("cm", 100.0),
}

# Business rule for strict requested/offered compensation:
# only liquids and packaging are compensable.
# - liquids: l/cl/ml converted to ml
# - packaging: counts (BT100, B/100, boite de 100, 2*50 in packaging context)
_MATCH_COMPENSABLE_UNIT_TO_BASE = {
    "ml": ("ml", 1.0),
    "cl": ("ml", 10.0),
    "l": ("ml", 1000.0),
}

_NON_COMPENSABLE_LENGTH_UNITS = {"m", "cm", "mm"}
_NON_COMPENSABLE_WEIGHT_UNITS = {"kg", "g", "mg"}

_INFERRED_UNIT_ALIASES = {
    "u": "un",
    "un": "un",
    "unit": "un",
    "unite": "un",
    "unites": "un",
    "piece": "un",
    "pieces": "un",
    "pc": "un",
    "pcs": "un",
    "rouleau": "rouleau",
    "rouleaux": "rouleau",
    "roll": "rouleau",
    "rolls": "rouleau",
}

_ALLOWED_INFERRED_UNITS = {
    "mm",
    "cm",
    "m",
    "mg",
    "g",
    "kg",
    "ml",
    "cl",
    "l",
}

_PACKAGING_ONLY_UNITS = {
    "u",
    "un",
    "unit",
    "unite",
    "unites",
    "piece",
    "pieces",
    "pc",
    "pcs",
    "rouleau",
    "rouleaux",
    "roll",
    "rolls",
    "flacon",
    "flacons",
    "boite",
    "boites",
    "box",
    "pack",
    "paquet",
    "carton",
    "sachet",
    "sachets",
    "bt",
    "bte",
}

_ACCESSORY_TOKENS = {
    "chariot",
    "charriot",
    "trolley",
    "sutrolley",
    "support",
    "panier",
    "socle",
    "table",
    "tiroir",
    "roulette",
}

_TECH_SIMILARITY_WEIGHTS = {
    "characteristics": 0.45,
    "requirements": 0.25,
    "accessories": 0.20,
    "description": 0.10,
}
_TECH_SIMILARITY_PROMPT_VERSION = "fr_v1"
_TECH_SIMILARITY_MATCH_THRESHOLD = 0.40
_TECH_SIMILARITY_STATUS_CONFORM_THRESHOLD = 0.80
_TECH_SIMILARITY_STATUS_PARTIAL_THRESHOLD = 0.60
_TECH_SIMILARITY_DISPLAY_PARTIAL_FLOOR = _TECH_SIMILARITY_STATUS_PARTIAL_THRESHOLD
_TECH_SIMILARITY_DISPLAY_LINK_BONUS = 0.05
_TECH_SIMILARITY_DISPLAY_STRONG_COMPONENT_COVERAGE = 0.40
_TECH_SIMILARITY_DISPLAY_TECH_WEIGHT = 0.70
_TECH_SIMILARITY_DISPLAY_GPT_MATCH_WEIGHT = 0.30
_TECH_SIMILARITY_DISPLAY_MISSING_EVIDENCE_WEIGHT_FACTOR = 0.35

# In-process cache to avoid repeated GPT tech-similarity calls on every UI refresh.
# key -> (timestamp_epoch_seconds, similarity_payload)
_TECH_SIMILARITY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TECH_SIMILARITY_CACHE_TTL_SECONDS = 3600
_TECH_SIMILARITY_CACHE_MAX_ENTRIES = 8000
_INVOICE_COMPONENTS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INVOICE_COMPONENTS_CACHE_TTL_SECONDS = 3600
_INVOICE_COMPONENTS_CACHE_MAX_ENTRIES = 8000
_OFFER_ANALYSIS_CACHE_VERSION = 1
_STRICT_INVOICE_OCR_CACHE: dict[int, tuple[float, str]] = {}
_STRICT_INVOICE_OCR_CACHE_TTL_SECONDS = 3600
_STRICT_INVOICE_OCR_CACHE_MAX_ENTRIES = 200
_STRICT_INVOICE_GLOBAL_VAT_CACHE: dict[int, tuple[float, dict[str, Any] | None]] = {}
_STRICT_INVOICE_GLOBAL_VAT_CACHE_TTL_SECONDS = 3600
_STRICT_INVOICE_GLOBAL_VAT_CACHE_MAX_ENTRIES = 200
_STRICT_INVOICE_NUMERIC_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STRICT_INVOICE_NUMERIC_CACHE_TTL_SECONDS = 3600
_STRICT_INVOICE_NUMERIC_CACHE_MAX_ENTRIES = 500
_ACCESSORY_KEYS = {
    "accessoire",
    "accessoires",
    "accessoires_obligatoires",
    "accessoires structures",
    "accessoires_structures",
}
_REQUIREMENT_KEYS = {
    "exigence",
    "exigences",
    "requirement",
    "requirements",
    "contraintes",
    "obligatoire",
}
_DESCRIPTION_KEYS = {
    "description",
    "details",
    "detail",
    "notes",
    "observations",
}
_REQUIREMENT_LINE_HINTS = (
    "doit",
    "oblig",
    "conforme",
    "certificat",
    "garantie",
    "norme",
    "livre avec",
    "fourni avec",
    "must",
    "required",
)
_ACCESSORY_LINE_HINTS = (
    "accessoire",
    "kit",
    "cordon",
    "plateau",
    "panier",
    "canule",
    "pompe",
    "tabouret",
    "meuble",
    "support",
    "chariot",
    "embout",
)


def _ascii_norm(text: str | None) -> str:
    raw = _norm(text)
    if not raw:
        return ""
    return (
        unicodedata.normalize("NFKD", raw)
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def _dedupe_texts(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = re.sub(r"\s+", " ", str(raw or "").strip())
        if not text:
            continue
        key = _ascii_norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _collect_text_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = re.sub(r"\s+", " ", raw.strip())
        return [text] if text else []
    if isinstance(raw, (int, float, bool)):
        return [str(raw)]
    out: list[str] = []
    if isinstance(raw, (list, tuple, set)):
        for entry in raw:
            out.extend(_collect_text_values(entry))
        return _dedupe_texts(out)
    if isinstance(raw, dict):
        # Keep explicit text fields first when present.
        explicit = []
        for key in ("text", "item", "details", "value", "name", "material", "group"):
            if key in raw:
                explicit.extend(_collect_text_values(raw.get(key)))
        if explicit:
            out.extend(explicit)
        else:
            for value in raw.values():
                out.extend(_collect_text_values(value))
        return _dedupe_texts(out)
    return []


def _normalize_char_lines_with_attachment_schema(raw_characteristics: Any) -> list[str]:
    char_map: dict[str, Any] = {}
    if isinstance(raw_characteristics, dict):
        for raw_key, raw_value in raw_characteristics.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            char_map[key] = raw_value
    elif isinstance(raw_characteristics, list):
        for row in raw_characteristics:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            if not key:
                continue
            values = row.get("values")
            if values is None:
                values = row.get("value")
            char_map[key] = values
    else:
        return _collect_text_values(raw_characteristics)

    if not char_map:
        return []

    try:
        from app.services.campaign_attachment_extraction import _normalize_characteristics_payload

        normalized_rows = _normalize_characteristics_payload(char_map, char_map)
        out: list[str] = []
        for row in normalized_rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            values = _collect_text_values(row.get("values"))
            if values:
                for value in values:
                    out.append(f"{key}: {value}" if key else value)
            elif key:
                out.append(key)
        return _dedupe_texts(out)
    except Exception:
        out: list[str] = []
        for raw_key, raw_value in char_map.items():
            key = str(raw_key or "").strip()
            values = _collect_text_values(raw_value)
            if values:
                for value in values:
                    out.append(f"{key}: {value}" if key else value)
            elif key:
                out.append(key)
        return _dedupe_texts(out)


def _invoice_components_has_signal(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get("characteristics")
        or payload.get("requirements")
        or payload.get("accessories")
        or payload.get("description")
    )


def _invoice_components_cache_key(block_lines: list[str]) -> str:
    joined = _ascii_norm(" || ".join(block_lines))
    if not joined:
        return ""
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _invoice_components_cache_get(key: str) -> dict[str, Any] | None:
    if not key:
        return None
    row = _INVOICE_COMPONENTS_CACHE.get(key)
    if row:
        ts, value = row
        now = time.time()
        if (now - ts) <= _INVOICE_COMPONENTS_CACHE_TTL_SECONDS:
            return value
        _INVOICE_COMPONENTS_CACHE.pop(key, None)
    model = (
        str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "") or "").strip()
        or "gpt-5.1"
    )
    fallback_model = str(
        getattr(config, "OPENAI_INVOICE_COMPONENTS_FALLBACK_MODEL", "") or ""
    ).strip()
    model_candidates: list[str] = [model]
    if fallback_model and fallback_model != model:
        model_candidates.append(fallback_model)
    for model_name in model_candidates:
        cached = get_openai_payload_cache(
            cache_type="invoice_components",
            model=model_name,
            cache_key=key,
        )
        if isinstance(cached, dict):
            _INVOICE_COMPONENTS_CACHE[key] = (time.time(), cached)
            return cached
    return None


def _invoice_components_cache_set(
    key: str,
    value: dict[str, Any],
    *,
    model_override: str | None = None,
) -> None:
    if not key or not isinstance(value, dict):
        return
    _INVOICE_COMPONENTS_CACHE[key] = (time.time(), value)
    model = str(model_override or "").strip() or (
        str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "") or "").strip()
        or "gpt-5.1"
    )
    set_openai_payload_cache(
        cache_type="invoice_components",
        model=model,
        cache_key=key,
        payload=value,
    )
    if len(_INVOICE_COMPONENTS_CACHE) <= _INVOICE_COMPONENTS_CACHE_MAX_ENTRIES:
        return
    keep_n = int(_INVOICE_COMPONENTS_CACHE_MAX_ENTRIES * 0.9)
    ordered = sorted(_INVOICE_COMPONENTS_CACHE.items(), key=lambda kv: kv[1][0])
    newest = ordered[-max(1, keep_n) :]
    _INVOICE_COMPONENTS_CACHE.clear()
    _INVOICE_COMPONENTS_CACHE.update({k: v for k, v in newest})


def _get_invoice_ocr_text(
    invoice: InboundInvoice | None,
    *,
    max_chars: int,
) -> str:
    if invoice is None or not invoice.file_path:
        return ""
    stored_snapshot = str(getattr(invoice, "ocr_text_snapshot", "") or "").strip()
    if stored_snapshot:
        clean_stored = stored_snapshot[-max_chars:] if (max_chars and len(stored_snapshot) > max_chars) else stored_snapshot
        _STRICT_INVOICE_OCR_CACHE[invoice.id] = (time.time(), clean_stored)
        return clean_stored
    cached = _STRICT_INVOICE_OCR_CACHE.get(invoice.id)
    if cached:
        ts, value = cached
        if (time.time() - ts) <= _STRICT_INVOICE_OCR_CACHE_TTL_SECONDS:
            return value
        _STRICT_INVOICE_OCR_CACHE.pop(invoice.id, None)

    text = ""
    processing_file_path = resolve_invoice_processing_file_path(invoice.file_path) or invoice.file_path
    try:
        text = _extract_pdf_text(str(processing_file_path)) or ""
    except Exception:
        text = ""
    if not text:
        ocr_provider = str(getattr(config, "OPENAI_OCR_PROVIDER", "document_ai") or "document_ai").strip().lower()
        if ocr_provider in {"gpt4_vision", "openai_vision", "vision"}:
            try:
                from app.services.openai_vision_ocr import extract_text_from_file

                text = extract_text_from_file(
                    file_path=str(processing_file_path),
                    operation="invoice_ocr_openai_vision",
                    max_pages=getattr(config, "OPENAI_OCR_VISION_MAX_PAGES", 8),
                )
            except Exception:
                text = ""
    if not text:
        try:
            from app.services.document_ai import process_invoice

            doc = process_invoice(str(processing_file_path))
            text = str(getattr(doc, "text", None) or "").strip()
        except Exception:
            text = ""
    if not text:
        try:
            from app.services.document_ai import extract_full_text

            text = str(extract_full_text(str(processing_file_path)) or "").strip()
        except Exception:
            text = ""

    if not text:
        return ""

    clean = str(text).strip()
    if max_chars and len(clean) > max_chars:
        # Keep the tail to maximize chances of totals/TVA appearing.
        clean = clean[-max_chars:]

    # Persist OCR snapshot once so subsequent strict runs do not recall OCR providers.
    if clean and not str(getattr(invoice, "ocr_text_snapshot", "") or "").strip():
        try:
            invoice.ocr_text_snapshot = clean
        except Exception:
            pass

    _STRICT_INVOICE_OCR_CACHE[invoice.id] = (time.time(), clean)
    if len(_STRICT_INVOICE_OCR_CACHE) > _STRICT_INVOICE_OCR_CACHE_MAX_ENTRIES:
        ordered = sorted(_STRICT_INVOICE_OCR_CACHE.items(), key=lambda kv: kv[1][0])
        keep_n = int(_STRICT_INVOICE_OCR_CACHE_MAX_ENTRIES * 0.9)
        keep = ordered[-max(1, keep_n) :]
        _STRICT_INVOICE_OCR_CACHE.clear()
        _STRICT_INVOICE_OCR_CACHE.update({k: v for k, v in keep})
    return clean


def _strict_payload_has_vat(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return payload.get("vat_rate") is not None


def _is_reliable_db_unit_price(item: InvoiceExtractionItem) -> bool:
    if item.unit_price is None:
        return False
    notes = str(item.notes or "").lower()
    if "price_source=gpt_fallback" in notes:
        return False
    if "price_source=derived_amount_div_qty" in notes:
        # Accept derived prices only when internally consistent with stored line amount.
        qty = float(item.quantity) if isinstance(item.quantity, (int, float)) else None
        pu = float(item.unit_price) if isinstance(item.unit_price, (int, float)) else None
        amount_match = re.search(r"\bline_amount=([0-9]+(?:\.[0-9]+)?)", notes)
        if qty is None or pu is None or qty <= 0 or pu <= 0 or not amount_match:
            return False
        try:
            amount = float(amount_match.group(1))
        except Exception:
            return False
        if amount <= 0:
            return False
        rel_err = abs((qty * pu) - amount) / max(abs(amount), 1.0)
        return rel_err <= 0.005
    if "price_source=amount_only" in notes:
        return False
    # Keep explicit parser/ocr sources.
    if "price_source=ocr_unit_price" in notes:
        return True
    if "price_source=local_table" in notes:
        return True
    if "source_engine=xlsx_parser" in str(item.source_engine or "").lower():
        return True
    # Conservative default: do not force when unknown.
    return False


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    raw = str(value).strip().replace(" ", "")
    if not raw:
        return None
    raw = raw.replace(",", ".")
    try:
        v = float(raw)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def _extract_line_amount_from_notes(notes: str | None) -> float | None:
    if not notes:
        return None
    match = re.search(r"\bline_amount=([0-9]+(?:\.[0-9]+)?)", str(notes))
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _extract_note_token(notes: str | None, key: str) -> str | None:
    if not notes:
        return None
    pattern = rf"\b{re.escape(key)}=([^\s;]+)"
    match = re.search(pattern, str(notes))
    if not match:
        return None
    value = str(match.group(1) or "").strip()
    return value or None


def _number_type_from_raw(raw_text: str | None, value: float | None) -> str | None:
    raw = str(raw_text or "").strip()
    if raw:
        if re.search(r"\d[.,]\d", raw):
            return "decimal"
        if re.search(r"\d", raw):
            return "integer"
    if value is None:
        return None
    return "integer" if float(value).is_integer() else "decimal"


def _line_financially_consistent(
    quantity: float | None,
    unit_price: float | None,
    line_total: float | None,
    *,
    tolerance_ratio: float = 0.005,
) -> bool:
    if quantity is None or unit_price is None or line_total is None:
        return False
    if quantity <= 0 or unit_price <= 0 or line_total <= 0:
        return False
    rel_err = abs((quantity * unit_price) - line_total) / max(abs(line_total), 1.0)
    return rel_err <= tolerance_ratio


def _parser_numeric_from_notes(item: InvoiceExtractionItem) -> dict[str, Any]:
    notes = str(item.notes or "")
    qty = _safe_float(item.quantity)
    unit_price = _safe_float(item.unit_price)
    line_total = _extract_line_amount_from_notes(notes)

    qty_raw = _extract_note_token(notes, "qty_raw")
    unit_price_raw = _extract_note_token(notes, "unit_price_raw")
    line_total_raw = _extract_note_token(notes, "line_total_raw")

    if line_total is None:
        line_total = _safe_float(line_total_raw)

    qty_num_type = _extract_note_token(notes, "qty_num_type") or _number_type_from_raw(qty_raw, qty)
    unit_price_num_type = _extract_note_token(notes, "unit_price_num_type") or _number_type_from_raw(
        unit_price_raw, unit_price
    )
    line_total_num_type = _extract_note_token(notes, "line_total_num_type") or _number_type_from_raw(
        line_total_raw, line_total
    )

    return {
        "quantity": qty,
        "unit_price": unit_price,
        "line_total": line_total,
        "price_source": _extract_note_token(notes, "price_source"),
        "quantity_raw": qty_raw,
        "unit_price_raw": unit_price_raw,
        "line_total_raw": line_total_raw,
        "quantity_num_type": qty_num_type,
        "unit_price_num_type": unit_price_num_type,
        "line_total_num_type": line_total_num_type,
    }


def _apply_parser_numeric_rules_to_payload(
    item: InvoiceExtractionItem,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    numeric = _parser_numeric_from_notes(item)
    parser_qty = _safe_float(numeric.get("quantity"))
    parser_unit_price = _safe_float(numeric.get("unit_price"))
    line_total = _safe_float(numeric.get("line_total"))
    price_source = str(numeric.get("price_source") or "").strip().lower()
    qty_num_type = str(numeric.get("quantity_num_type") or "").strip().lower() or None
    unit_price_num_type = str(numeric.get("unit_price_num_type") or "").strip().lower() or None

    final_qty = parser_qty
    final_unit_price = parser_unit_price
    swap_applied = False
    swap_reason: str | None = None
    if (
        parser_qty is not None
        and parser_unit_price is not None
        and line_total is not None
        and price_source in {"derived_amount_div_qty", "amount_only"}
        and qty_num_type == "decimal"
        and unit_price_num_type == "integer"
    ):
        candidate_qty = parser_unit_price
        candidate_unit_price = parser_qty
        if _line_financially_consistent(candidate_qty, candidate_unit_price, line_total):
            final_qty = candidate_qty
            final_unit_price = candidate_unit_price
            swap_applied = True
            swap_reason = "qty_decimal_unit_price_integer"

    if payload.get("numeric_source") != "gpt_invoice_global":
        if final_qty is not None:
            payload["quantity"] = final_qty
        if _is_reliable_db_unit_price(item) and final_unit_price is not None:
            payload["unit_price"] = final_unit_price
        if line_total is not None and payload.get("line_total") is None:
            payload["line_total"] = line_total
        if swap_applied and not str(payload.get("numeric_source") or "").strip():
            payload["numeric_source"] = "parser_swap"

    trace = dict(payload.get("numeric_trace") or {})
    trace["quantity_raw"] = numeric.get("quantity_raw")
    trace["quantity_num_type"] = numeric.get("quantity_num_type")
    trace["unit_price_raw"] = numeric.get("unit_price_raw")
    trace["unit_price_num_type"] = numeric.get("unit_price_num_type")
    trace["line_total_raw"] = numeric.get("line_total_raw")
    trace["line_total_num_type"] = numeric.get("line_total_num_type")
    trace["source"] = "parser_notes"
    trace["swap_applied"] = bool(swap_applied)
    trace["swap_reason"] = swap_reason
    trace["validated_by_formula"] = (
        _line_financially_consistent(final_qty, final_unit_price, line_total) if line_total is not None else None
    )
    payload["numeric_trace"] = trace
    return payload


def _invoice_numeric_cache_get(cache_key: str, model_candidates: list[str]) -> dict[str, Any] | None:
    if not cache_key:
        return None
    row = _STRICT_INVOICE_NUMERIC_CACHE.get(cache_key)
    if row:
        ts, value = row
        if (time.time() - ts) <= _STRICT_INVOICE_NUMERIC_CACHE_TTL_SECONDS:
            return value
        _STRICT_INVOICE_NUMERIC_CACHE.pop(cache_key, None)
    for model_name in model_candidates:
        cached = get_openai_payload_cache(
            cache_type="invoice_numeric_completion",
            model=model_name,
            cache_key=cache_key,
        )
        if isinstance(cached, dict):
            _STRICT_INVOICE_NUMERIC_CACHE[cache_key] = (time.time(), cached)
            return cached
    return None


def _invoice_numeric_cache_set(cache_key: str, value: dict[str, Any], model: str) -> None:
    if not cache_key or not isinstance(value, dict):
        return
    _STRICT_INVOICE_NUMERIC_CACHE[cache_key] = (time.time(), value)
    set_openai_payload_cache(
        cache_type="invoice_numeric_completion",
        model=model,
        cache_key=cache_key,
        payload=value,
    )
    if len(_STRICT_INVOICE_NUMERIC_CACHE) <= _STRICT_INVOICE_NUMERIC_CACHE_MAX_ENTRIES:
        return
    keep_n = int(_STRICT_INVOICE_NUMERIC_CACHE_MAX_ENTRIES * 0.9)
    ordered = sorted(_STRICT_INVOICE_NUMERIC_CACHE.items(), key=lambda kv: kv[1][0])
    keep = ordered[-max(1, keep_n) :]
    _STRICT_INVOICE_NUMERIC_CACHE.clear()
    _STRICT_INVOICE_NUMERIC_CACHE.update({k: v for k, v in keep})


def _invoice_numeric_cache_key(
    *,
    invoice_id: int,
    parser_rows: list[dict[str, Any]],
    invoice_text: str,
) -> str:
    snapshot = {
        "invoice_id": int(invoice_id),
        "rows": [
            {
                "item_id": row.get("item_id"),
                "product_name": row.get("product_name"),
                "quantity": row.get("quantity"),
                "unit_price": row.get("unit_price"),
                "line_total": row.get("line_total"),
            }
            for row in parser_rows
        ],
        "invoice_text_hash": hashlib.sha256(str(invoice_text or "").encode("utf-8")).hexdigest(),
    }
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_invoice_numeric_completion_with_gpt(
    invoice: InboundInvoice | None,
    invoice_items: list[InvoiceExtractionItem],
) -> dict[int, dict[str, Any]]:
    if invoice is None or invoice.id is None:
        return {}
    if not config.OPENAI_API_KEY:
        return {}
    mode = str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODE", "local") or "local").strip().lower()
    if mode in {"off", "disabled", "local", "regex"}:
        return {}
    if not invoice_items:
        return {}

    parser_rows: list[dict[str, Any]] = []
    needs_completion = False
    for it in invoice_items:
        parser_numeric = _parser_numeric_from_notes(it)
        line_total = _safe_float(parser_numeric.get("line_total"))
        qty = _safe_float(parser_numeric.get("quantity"))
        unit_price = _safe_float(parser_numeric.get("unit_price"))
        consistent = _line_financially_consistent(qty, unit_price, line_total)
        source_match = re.search(r"\bprice_source=([A-Za-z0-9_\-]+)", str(it.notes or ""))
        row = {
            "item_id": it.id,
            "product_name": str(it.product_name or "").strip(),
            "description": str(it.description or "").strip() or None,
            "raw_description": str(it.raw_description or "").strip() or None,
            "quantity": qty,
            "quantity_raw": parser_numeric.get("quantity_raw"),
            "quantity_num_type": parser_numeric.get("quantity_num_type"),
            "unit_price": unit_price,
            "unit_price_raw": parser_numeric.get("unit_price_raw"),
            "unit_price_num_type": parser_numeric.get("unit_price_num_type"),
            "line_total": line_total,
            "line_total_raw": parser_numeric.get("line_total_raw"),
            "line_total_num_type": parser_numeric.get("line_total_num_type"),
            "parser_price_source": source_match.group(1) if source_match else None,
            "needs_completion": (qty is None or unit_price is None or line_total is None or not consistent),
        }
        if row["needs_completion"]:
            needs_completion = True
        parser_rows.append(row)

    if not needs_completion:
        return {}

    max_chars = max(1200, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS", 5000) or 5000))
    invoice_text = _get_invoice_ocr_text(invoice, max_chars=max_chars)
    if not invoice_text:
        return {}

    model = (
        str(getattr(config, "OPENAI_INVOICE_COMPONENTS_STRICT_MODEL", "") or "").strip()
        or str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "") or "").strip()
        or "gpt-5.1"
    )
    fallback_model = str(getattr(config, "OPENAI_INVOICE_COMPONENTS_FALLBACK_MODEL", "") or "").strip()
    model_candidates: list[str] = [model]
    if fallback_model and fallback_model != model:
        model_candidates.append(fallback_model)

    cache_key = _invoice_numeric_cache_key(
        invoice_id=int(invoice.id),
        parser_rows=parser_rows,
        invoice_text=invoice_text,
    )
    cached = _invoice_numeric_cache_get(cache_key, model_candidates)
    if isinstance(cached, dict):
        rows = cached.get("rows")
        if isinstance(rows, list):
            out: dict[int, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item_id = _safe_float(row.get("item_id"))
                if item_id is None:
                    continue
                out[int(item_id)] = row
            return out

    timeout_s = max(15, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45))
    system_prompt = (
        "You reconcile invoice numeric fields per line. "
        "Use only provided parser_rows and invoice_ocr_text. "
        "Goal: resolve missing or inconsistent quantity, unit_price, line_total. "
        "You may swap quantity/unit_price only if this improves financial consistency. "
        "Validation rule: line_total ~= quantity * unit_price with max relative error 0.5%. "
        "Keep uncertain fields null. "
        "Return strict JSON with a rows array keyed by item_id."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "invoice_ocr_text": invoice_text,
                        "parser_rows": parser_rows,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_numeric_completion",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "item_id": {"type": "number"},
                                    "quantity_raw": {"type": ["string", "null"]},
                                    "quantity": {"type": ["number", "null"]},
                                    "quantity_num_type": {"type": ["string", "null"]},
                                    "unit_price_raw": {"type": ["string", "null"]},
                                    "unit_price": {"type": ["number", "null"]},
                                    "unit_price_num_type": {"type": ["string", "null"]},
                                    "line_total_raw": {"type": ["string", "null"]},
                                    "line_total": {"type": ["number", "null"]},
                                    "line_total_num_type": {"type": ["string", "null"]},
                                    "confidence": {"type": ["number", "null"]},
                                    "reason": {"type": ["string", "null"]},
                                },
                                "required": [
                                    "item_id",
                                    "quantity_raw",
                                    "quantity",
                                    "quantity_num_type",
                                    "unit_price_raw",
                                    "unit_price",
                                    "unit_price_num_type",
                                    "line_total_raw",
                                    "line_total",
                                    "line_total_num_type",
                                    "confidence",
                                    "reason",
                                ],
                            },
                        }
                    },
                    "required": ["rows"],
                },
            },
        },
    }
    parsed, used_model = call_chat_completion_json_with_retry(
        operation="invoice_numeric_completion_gpt",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=timeout_s,
        retry_attempts=max(1, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3)),
        retry_backoff_s=max(
            0.2,
            float(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0) or 1.0),
        ),
        metadata={"invoice_id": int(invoice.id), "row_count": len(parser_rows)},
    )
    if not isinstance(parsed, dict):
        return {}
    rows = parsed.get("rows")
    if not isinstance(rows, list):
        return {}

    model_used = str(used_model or model).strip() or model
    _invoice_numeric_cache_set(cache_key, parsed, model_used)
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = _safe_float(row.get("item_id"))
        if item_id is None:
            continue
        out[int(item_id)] = row
    return out


def _apply_invoice_numeric_completion(
    invoice_items: list[InvoiceExtractionItem],
    completion_rows: dict[int, dict[str, Any]],
) -> None:
    if not invoice_items or not completion_rows:
        return
    for it in invoice_items:
        if it.id is None:
            continue
        row = completion_rows.get(int(it.id))
        if not isinstance(row, dict):
            continue
        payload = dict(it.strict_components_payload or {})
        parser_numeric = _parser_numeric_from_notes(it)

        g_qty = _safe_float(row.get("quantity"))
        g_unit_price = _safe_float(row.get("unit_price"))
        g_total = _safe_float(row.get("line_total"))
        parser_total = _safe_float(parser_numeric.get("line_total"))

        candidate_total = g_total if g_total is not None else parser_total
        g_consistent = _line_financially_consistent(g_qty, g_unit_price, candidate_total)

        if g_consistent and g_qty is not None and g_unit_price is not None:
            payload["quantity"] = g_qty
            payload["unit_price"] = g_unit_price
            if g_total is not None:
                payload["line_total"] = g_total
            payload["numeric_source"] = "gpt_invoice_global"
        else:
            if payload.get("quantity") is None and g_qty is not None:
                payload["quantity"] = g_qty
            if payload.get("unit_price") is None and g_unit_price is not None:
                payload["unit_price"] = g_unit_price
            if payload.get("line_total") is None and g_total is not None:
                payload["line_total"] = g_total

        payload["numeric_trace"] = {
            "quantity_raw": row.get("quantity_raw") or parser_numeric.get("quantity_raw"),
            "quantity_num_type": row.get("quantity_num_type")
            or parser_numeric.get("quantity_num_type")
            or _number_type_from_raw(row.get("quantity_raw"), g_qty),
            "unit_price_raw": row.get("unit_price_raw") or parser_numeric.get("unit_price_raw"),
            "unit_price_num_type": row.get("unit_price_num_type")
            or parser_numeric.get("unit_price_num_type")
            or _number_type_from_raw(row.get("unit_price_raw"), g_unit_price),
            "line_total_raw": row.get("line_total_raw") or parser_numeric.get("line_total_raw"),
            "line_total_num_type": row.get("line_total_num_type")
            or parser_numeric.get("line_total_num_type")
            or _number_type_from_raw(row.get("line_total_raw"), g_total),
            "confidence": _safe_float(row.get("confidence")),
            "reason": row.get("reason"),
            "validated_by_formula": bool(g_consistent),
            "source": "gpt_invoice_global",
        }
        payload = _ensure_unit_missing_flag(payload)
        it.strict_components_payload = payload


def _extract_invoice_global_vat_with_gpt(invoice_text: str) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEY:
        return None
    mode = str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODE", "local") or "local").strip().lower()
    if mode in {"off", "disabled", "local", "regex"}:
        return None
    text_for_model = str(invoice_text or "").strip()
    if not text_for_model:
        return None
    max_chars = max(1200, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS", 5000) or 5000))
    if len(text_for_model) > max_chars:
        text_for_model = text_for_model[-max_chars:]

    model = (
        str(getattr(config, "OPENAI_INVOICE_COMPONENTS_STRICT_MODEL", "") or "").strip()
        or str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "") or "").strip()
        or "gpt-5.1"
    )
    timeout_s = max(15, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45))
    system_prompt = (
        "Extract GLOBAL VAT rate for the invoice from OCR text. "
        "Return strict JSON keys: vat_rate, vat_rate_calc. "
        "Priority: explicit VAT percentage -> vat_rate. "
        "If no explicit rate, compute from HT/TVA/TTC -> vat_rate_calc. "
        "If multiple rates or unreliable evidence, return nulls."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps({"invoice_ocr_text": text_for_model}, ensure_ascii=False),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_global_vat",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "vat_rate": {"type": ["number", "null"]},
                        "vat_rate_calc": {"type": ["number", "null"]},
                    },
                    "required": ["vat_rate", "vat_rate_calc"],
                },
            },
        },
    }
    parsed, _ = call_chat_completion_json_with_retry(
        operation="invoice_components_global_vat_gpt",
        payload=payload,
        primary_model=model,
        fallback_model=str(
            getattr(config, "OPENAI_INVOICE_COMPONENTS_FALLBACK_MODEL", "") or ""
        ).strip(),
        timeout_s=timeout_s,
        retry_attempts=max(
            1,
            int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3),
        ),
        retry_backoff_s=max(
            0.2,
            float(
                getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0)
                or 1.0
            ),
        ),
        metadata={"text_len": len(text_for_model)},
    )
    if not isinstance(parsed, dict):
        return None
    if parsed.get("vat_rate") is None and parsed.get("vat_rate_calc") is None:
        return None
    if parsed.get("vat_rate") is None and parsed.get("vat_rate_calc") is not None:
        parsed["vat_rate"] = parsed.get("vat_rate_calc")
        parsed["vat_source"] = "calculated_global"
    elif parsed.get("vat_rate") is not None and not str(parsed.get("vat_source") or "").strip():
        parsed["vat_source"] = "explicit_global"
    return parsed


def _get_invoice_global_vat(invoice: InboundInvoice | None, *, max_chars: int) -> dict[str, Any] | None:
    if invoice is None:
        return None
    cached = _STRICT_INVOICE_GLOBAL_VAT_CACHE.get(invoice.id)
    if cached:
        ts, payload = cached
        if (time.time() - ts) <= _STRICT_INVOICE_GLOBAL_VAT_CACHE_TTL_SECONDS:
            return payload
        _STRICT_INVOICE_GLOBAL_VAT_CACHE.pop(invoice.id, None)
    invoice_text = _get_invoice_ocr_text(invoice, max_chars=max_chars)
    payload = _extract_invoice_global_vat_with_gpt(invoice_text) if invoice_text else None
    _STRICT_INVOICE_GLOBAL_VAT_CACHE[invoice.id] = (time.time(), payload)
    if len(_STRICT_INVOICE_GLOBAL_VAT_CACHE) > _STRICT_INVOICE_GLOBAL_VAT_CACHE_MAX_ENTRIES:
        ordered = sorted(_STRICT_INVOICE_GLOBAL_VAT_CACHE.items(), key=lambda kv: kv[1][0])
        keep_n = int(_STRICT_INVOICE_GLOBAL_VAT_CACHE_MAX_ENTRIES * 0.9)
        keep = ordered[-max(1, keep_n) :]
        _STRICT_INVOICE_GLOBAL_VAT_CACHE.clear()
        _STRICT_INVOICE_GLOBAL_VAT_CACHE.update({k: v for k, v in keep})
    return payload


def _normalize_invoice_components_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "characteristics": [],
            "requirements": [],
            "accessories": [],
            "description": None,
        }

    char_raw = raw.get("characteristics")
    if char_raw is None:
        char_raw = raw.get("caracteristiques")
    req_raw = raw.get("requirements")
    if req_raw is None:
        req_raw = raw.get("exigences")
    acc_raw = raw.get("accessories")
    if acc_raw is None:
        acc_raw = raw.get("accessoires_structures")
    desc_raw = raw.get("description")

    characteristics = _normalize_char_lines_with_attachment_schema(char_raw)
    requirements = _dedupe_texts(_collect_text_values(req_raw))
    accessories = _dedupe_texts(_collect_text_values(acc_raw))
    desc_chunks = _dedupe_texts(_collect_text_values(desc_raw))
    description = " | ".join(desc_chunks).strip() or None
    return {
        "characteristics": characteristics,
        "requirements": requirements,
        "accessories": accessories,
        "description": description,
    }


def _extract_invoice_components_from_notes(notes: str | None) -> dict[str, Any] | None:
    if not notes:
        return None
    match = re.search(r"\binv_components_b64=([A-Za-z0-9_\-]+=*)", notes)
    if not match:
        return None
    raw_b64 = match.group(1).strip()
    if not raw_b64:
        return None
    try:
        padded = raw_b64 + ("=" * ((4 - len(raw_b64) % 4) % 4))
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="ignore")
        parsed = json.loads(payload)
    except Exception:
        return None
    normalized = _normalize_invoice_components_payload(parsed)
    return normalized if _invoice_components_has_signal(normalized) else None


def _preserve_invoice_components_notes_token(notes: str | None) -> str | None:
    payload = _extract_invoice_components_from_notes(notes)
    if payload is None:
        return None
    return _upsert_notes_b64_payload(None, "inv_components_b64", payload)


def _upsert_notes_b64_payload(notes: str | None, token_key: str, payload: dict[str, Any]) -> str:
    base_notes = str(notes or "").strip()
    token_pattern = rf"\b{re.escape(token_key)}=[A-Za-z0-9_\-]+=*"
    base_notes = re.sub(token_pattern, "", base_notes).strip()
    base_notes = re.sub(r"\s+", " ", base_notes).strip()
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(compact.encode("utf-8")).decode("ascii").rstrip("=")
    token = f"{token_key}={encoded}"
    return f"{base_notes} {token}".strip() if base_notes else token


def _extract_invoice_components_with_gpt(block_lines: list[str]) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEY:
        return None
    mode = str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODE", "local") or "local").strip().lower()
    if mode in {"off", "disabled", "local", "regex"}:
        return None

    model = (
        str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "") or "").strip()
        or "gpt-5.1"
    )
    timeout_s = max(15, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45))
    max_chars = max(1200, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS", 5000) or 5000))
    text_for_model = "\n".join(block_lines).strip()
    if not text_for_model:
        return None
    text_for_model = text_for_model[:max_chars]

    system_prompt = (
        "Extract structured offered product components from a single invoice line. "
        "Return strictly valid JSON with keys: characteristics, requirements, accessories, description. "
        "characteristics must be an array of objects: {key, value}. "
        "requirements/accessories must be arrays of short strings. "
        "description must be a short free-text string or null. "
        "Do not invent missing data."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"invoice_line_text": text_for_model},
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    parsed, _ = call_chat_completion_json_with_retry(
        operation="invoice_components_gpt",
        payload=payload,
        primary_model=model,
        fallback_model=str(
            getattr(config, "OPENAI_INVOICE_COMPONENTS_FALLBACK_MODEL", "") or ""
        ).strip(),
        timeout_s=timeout_s,
        retry_attempts=max(
            1,
            int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3),
        ),
        retry_backoff_s=max(
            0.2,
            float(
                getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0)
                or 1.0
            ),
        ),
        metadata={"line_count": len(block_lines)},
    )
    if not isinstance(parsed, dict):
        return None

    normalized = _normalize_invoice_components_payload(parsed)
    return normalized if _invoice_components_has_signal(normalized) else None


def _extract_invoice_components_strict_with_gpt(
    block_lines: list[str],
    *,
    invoice_text: str | None = None,
) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEY:
        return None
    mode = str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODE", "local") or "local").strip().lower()
    if mode in {"off", "disabled", "local", "regex"}:
        return None

    model = (
        str(getattr(config, "OPENAI_INVOICE_COMPONENTS_STRICT_MODEL", "") or "").strip()
        or str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "") or "").strip()
        or "gpt-5.1"
    )
    timeout_s = max(15, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45))
    max_chars = max(1200, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS", 5000) or 5000))
    text_for_model = "\n".join(block_lines).strip()
    if not text_for_model:
        return None
    text_for_model = text_for_model[:max_chars]
    invoice_text_for_model = ""
    if invoice_text:
        invoice_text_for_model = str(invoice_text).strip()
        if len(invoice_text_for_model) > max_chars:
            invoice_text_for_model = invoice_text_for_model[-max_chars:]

    system_prompt = (
        "You extract STRICT invoice data. Use invoice_line_text to extract the line item. "
        "Use invoice_ocr_text to detect the GLOBAL VAT rate for the whole invoice. "
        "Unit rules: unit must contain ONLY measurement units (mm, cm, m, mg, g, kg, ml, cl, l). "
        "If one of these units is explicit in the line, set unit and unit_source='explicit'. "
        "If measurement unit is missing but inferable from line context, keep unit=null, set unit_inferred and unit_source='inferred'. "
        "If unit cannot be inferred, set unit=null, unit_inferred=null, unit_source='missing'. "
        "Do not use conditionnement/packaging as unit (examples: BT100, B/100, boite, pack). "
        "Do not use commercial/count units as unit (examples: un, pcs, piece, rouleau, flacon). "
        "Unit price rules: never guess. Prefer explicit unit price markers (PU, P.U, Prix U, Unit Price). "
        "Treat quantity and unit_price as different fields. "
        "Do not copy an integer quantity into unit_price. "
        "When OCR shows money tokens with two decimals (examples: 140.00, 90.78, 90.65), "
        "prefer those tokens for unit_price. "
        "Assume invoice unit_price is monetary and formatted as decimal with two digits. "
        "If the only numeric candidate is integer-like and may be quantity, set unit_price=null. "
        "Do not swap quantity and unit_price: quantity is count, unit_price is money. "
        "If unit_price is not explicit, you may compute unit_price = line_amount / quantity only when both are present and consistent. "
        "If ambiguous, return unit_price=null. "
        "VAT rules: find explicit percent first (e.g., 'TVA 19%') -> vat_rate. "
        "If not explicit, compute from HT/TVA/TTC -> vat_rate_calc. "
        "If multiple distinct rates exist, return null for vat_rate and vat_rate_calc. "
        "If a field is missing or unclear, return null. Do not invent."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "invoice_line_text": text_for_model,
                        "invoice_ocr_text": invoice_text_for_model,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_line_strict",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "unit_missing": {"type": "boolean"},
                        "unit_inferred": {"type": ["string", "null"]},
                        "unit_source": {"type": ["string", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                        "packaging": {"type": ["string", "null"]},
                        "dimensions": {"type": ["string", "null"]},
                        "size": {"type": ["string", "null"]},
                        "weight": {"type": ["string", "null"]},
                        "vat_rate": {"type": ["number", "null"]},
                        "vat_rate_calc": {"type": ["number", "null"]},
                        "confidence": {"type": ["number", "null"]},
                    },
                    "required": [
                        "name",
                        "quantity",
                        "unit",
                        "unit_missing",
                        "unit_inferred",
                        "unit_source",
                        "unit_price",
                        "packaging",
                        "dimensions",
                        "size",
                        "weight",
                        "vat_rate",
                        "vat_rate_calc",
                        "confidence",
                    ],
                },
            },
        },
    }
    parsed, _ = call_chat_completion_json_with_retry(
        operation="invoice_components_strict_gpt",
        payload=payload,
        primary_model=model,
        fallback_model=str(
            getattr(config, "OPENAI_INVOICE_COMPONENTS_FALLBACK_MODEL", "") or ""
        ).strip(),
        timeout_s=timeout_s,
        retry_attempts=max(
            1,
            int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3),
        ),
        retry_backoff_s=max(
            0.2,
            float(
                getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0)
                or 1.0
            ),
        ),
        metadata={"line_count": len(block_lines)},
    )
    if not isinstance(parsed, dict):
        return None

    # Enforce deterministic unit-missing semantics for downstream badges/UI.
    unit_value = _normalize_measure_unit(parsed.get("unit"))
    if unit_value and _is_packaging_unit_value(unit_value):
        unit_value = None
    unit_inferred = _normalize_inferred_unit(parsed.get("unit_inferred"))
    parsed["unit_missing"] = False if unit_value else True
    if unit_value:
        parsed["unit"] = unit_value
        parsed["unit_inferred"] = None
        parsed["unit_source"] = "explicit"
    else:
        parsed["unit"] = None
        parsed["unit_inferred"] = unit_inferred
        parsed["unit_source"] = "inferred" if unit_inferred else "missing"
    return parsed


def _call_invoice_strict_json_schema(
    *,
    operation: str,
    schema_name: str,
    schema_properties: dict[str, Any],
    schema_required: list[str],
    system_prompt: str,
    user_payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEY:
        return None
    mode = str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODE", "local") or "local").strip().lower()
    if mode in {"off", "disabled", "local", "regex"}:
        return None

    model = (
        str(getattr(config, "OPENAI_INVOICE_COMPONENTS_STRICT_MODEL", "") or "").strip()
        or str(getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "") or "").strip()
        or "gpt-5.1"
    )
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": schema_properties,
                    "required": schema_required,
                },
            },
        },
    }
    parsed, _ = call_chat_completion_json_with_retry(
        operation=operation,
        payload=payload,
        primary_model=model,
        fallback_model=str(
            getattr(config, "OPENAI_INVOICE_COMPONENTS_FALLBACK_MODEL", "") or ""
        ).strip(),
        timeout_s=max(15, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_TIMEOUT_SECONDS", 45) or 45)),
        retry_attempts=max(
            1,
            int(getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_ATTEMPTS", 3) or 3),
        ),
        retry_backoff_s=max(
            0.2,
            float(
                getattr(config, "OPENAI_INVOICE_COMPONENTS_RETRY_BACKOFF_SECONDS", 1.0)
                or 1.0
            ),
        ),
        metadata=metadata or {},
    )
    return parsed if isinstance(parsed, dict) else None


def _extract_invoice_structure_split_with_gpt(
    block_lines: list[str],
) -> dict[str, Any] | None:
    text_for_model = "\n".join(block_lines).strip()
    if not text_for_model:
        return None
    max_chars = max(1200, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS", 5000) or 5000))
    text_for_model = text_for_model[:max_chars]
    system_prompt = (
        "You extract STRICT invoice product-line structure from invoice_line_text only. "
        "Return only: name, quantity, packaging, dimensions, size, weight, confidence. "
        "quantity is ordered count, never money, weight, or dimensions. "
        "packaging is conditionnement only (BT100, B/100, boite 100, pack 50, rouleau if commercial form). "
        "dimensions are product format-like dimensions (5x5, 7.5x7.5, 3M*10CM). "
        "weight is only explicit weight expressions (mg, g, kg). "
        "If ambiguous, return null. Do not infer unit_price or VAT. Do not invent."
    )
    return _call_invoice_strict_json_schema(
        operation="invoice_components_structure_split_gpt",
        schema_name="invoice_line_structure_split",
        schema_properties={
            "name": {"type": "string"},
            "quantity": {"type": ["number", "null"]},
            "packaging": {"type": ["string", "null"]},
            "dimensions": {"type": ["string", "null"]},
            "size": {"type": ["string", "null"]},
            "weight": {"type": ["string", "null"]},
            "confidence": {"type": ["number", "null"]},
        },
        schema_required=["name", "quantity", "packaging", "dimensions", "size", "weight", "confidence"],
        system_prompt=system_prompt,
        user_payload={"invoice_line_text": text_for_model},
        metadata={"line_count": len(block_lines), "split_stage": "structure"},
    )


def _extract_invoice_unit_split_with_gpt(
    block_lines: list[str],
) -> dict[str, Any] | None:
    text_for_model = "\n".join(block_lines).strip()
    if not text_for_model:
        return None
    max_chars = max(1200, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS", 5000) or 5000))
    text_for_model = text_for_model[:max_chars]
    system_prompt = (
        "You extract ONLY measurement-unit information from an invoice product line. "
        "Allowed measurement units: mm, cm, m, mg, g, kg, ml, cl, l. "
        "If a measurement unit is explicit, return unit and unit_source='explicit'. "
        "If not explicit but reliably inferable from the line, return unit=null, unit_inferred, unit_source='inferred'. "
        "If not reliable, return unit=null, unit_inferred=null, unit_source='missing'. "
        "Never use packaging as unit: BT100, B/100, boite, pack, sachet. "
        "Never use commercial/count units as measurement unit: piece, pcs, rouleau, flacon, unite. "
        "Never infer from packaging alone."
    )
    parsed = _call_invoice_strict_json_schema(
        operation="invoice_components_unit_split_gpt",
        schema_name="invoice_line_unit_split",
        schema_properties={
            "unit": {"type": ["string", "null"]},
            "unit_missing": {"type": "boolean"},
            "unit_inferred": {"type": ["string", "null"]},
            "unit_source": {"type": ["string", "null"]},
        },
        schema_required=["unit", "unit_missing", "unit_inferred", "unit_source"],
        system_prompt=system_prompt,
        user_payload={"invoice_line_text": text_for_model},
        metadata={"line_count": len(block_lines), "split_stage": "unit"},
    )
    if not isinstance(parsed, dict):
        return None
    return _ensure_unit_missing_flag(parsed)


def _extract_invoice_numeric_vat_split_with_gpt(
    block_lines: list[str],
    *,
    invoice_text: str | None = None,
) -> dict[str, Any] | None:
    text_for_model = "\n".join(block_lines).strip()
    if not text_for_model:
        return None
    max_chars = max(1200, int(getattr(config, "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS", 5000) or 5000))
    text_for_model = text_for_model[:max_chars]
    invoice_text_for_model = str(invoice_text or "").strip()
    if len(invoice_text_for_model) > max_chars:
        invoice_text_for_model = invoice_text_for_model[-max_chars:]
    system_prompt = (
        "You extract STRICT invoice numeric fields. "
        "Use invoice_line_text for line-level numeric extraction. "
        "Use invoice_ocr_text only for GLOBAL VAT detection. "
        "Return: quantity, unit_price, line_total, vat_rate, vat_rate_calc, numeric_confidence, numeric_reason. "
        "quantity is count, never money. unit_price is money, never count. "
        "Prefer explicit decimal money tokens for unit_price. "
        "If unit_price is not explicit, compute unit_price = line_total / quantity only when consistent. "
        "If ambiguous, return null. "
        "VAT: explicit percent first. Otherwise compute from HT/TVA/TTC. "
        "If multiple VAT rates exist, return null VAT fields. Do not invent."
    )
    return _call_invoice_strict_json_schema(
        operation="invoice_components_numeric_split_gpt",
        schema_name="invoice_line_numeric_split",
        schema_properties={
            "quantity": {"type": ["number", "null"]},
            "unit_price": {"type": ["number", "null"]},
            "line_total": {"type": ["number", "null"]},
            "vat_rate": {"type": ["number", "null"]},
            "vat_rate_calc": {"type": ["number", "null"]},
            "numeric_confidence": {"type": ["number", "null"]},
            "numeric_reason": {"type": ["string", "null"]},
        },
        schema_required=[
            "quantity",
            "unit_price",
            "line_total",
            "vat_rate",
            "vat_rate_calc",
            "numeric_confidence",
            "numeric_reason",
        ],
        system_prompt=system_prompt,
        user_payload={
            "invoice_line_text": text_for_model,
            "invoice_ocr_text": invoice_text_for_model,
        },
        metadata={"line_count": len(block_lines), "split_stage": "numeric_vat"},
    )


def _extract_invoice_components_strict_split_with_gpt(
    block_lines: list[str],
    *,
    invoice_text: str | None = None,
) -> dict[str, Any] | None:
    structure = _extract_invoice_structure_split_with_gpt(block_lines) or {}
    unit_payload = _extract_invoice_unit_split_with_gpt(block_lines) or {}
    numeric_payload = _extract_invoice_numeric_vat_split_with_gpt(
        block_lines,
        invoice_text=invoice_text,
    ) or {}
    if not any(isinstance(p, dict) and p for p in (structure, unit_payload, numeric_payload)):
        return None

    merged: dict[str, Any] = {
        "name": str(structure.get("name") or "").strip(),
        "quantity": numeric_payload.get("quantity", structure.get("quantity")),
        "unit": unit_payload.get("unit"),
        "unit_missing": bool(unit_payload.get("unit_missing")) if unit_payload else True,
        "unit_inferred": unit_payload.get("unit_inferred"),
        "unit_source": unit_payload.get("unit_source"),
        "unit_price": numeric_payload.get("unit_price"),
        "line_total": numeric_payload.get("line_total"),
        "packaging": structure.get("packaging"),
        "dimensions": structure.get("dimensions"),
        "size": structure.get("size"),
        "weight": structure.get("weight"),
        "vat_rate": numeric_payload.get("vat_rate"),
        "vat_rate_calc": numeric_payload.get("vat_rate_calc"),
        "confidence": structure.get("confidence"),
        "numeric_source": "gpt_split_test",
        "numeric_trace": {
            "confidence": _safe_float(numeric_payload.get("numeric_confidence")),
            "reason": numeric_payload.get("numeric_reason"),
            "validated_by_formula": None,
            "source": "gpt_split_test",
            "swap_applied": False,
            "swap_reason": None,
        },
    }
    merged = _ensure_unit_missing_flag(merged)
    return merged


def _build_invoice_item_block_lines(item: InvoiceExtractionItem) -> list[str]:
    base_name = re.sub(r"\s+", " ", str(item.product_name or "").strip())
    base_desc = re.sub(r"\s+", " ", str(item.description or "").strip())
    base_raw_desc = re.sub(r"\s+", " ", str(item.raw_description or "").strip())
    block_lines: list[str] = []
    if base_name:
        block_lines.append(base_name)
    if base_desc:
        for raw_line in re.split(r"[\r\n|]+", base_desc):
            line = re.sub(r"\s+", " ", str(raw_line or "").strip())
            if line:
                block_lines.append(line)
    if base_raw_desc:
        for raw_line in re.split(r"[\r\n|]+", base_raw_desc):
            line = re.sub(r"\s+", " ", str(raw_line or "").strip())
            if line:
                block_lines.append(line)
    return block_lines


def _ensure_unit_missing_flag(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    unit_value = _normalize_measure_unit(payload.get("unit"))
    if unit_value and _is_packaging_unit_value(unit_value):
        unit_value = None
    unit_inferred = _normalize_inferred_unit(payload.get("unit_inferred"))
    if unit_value:
        payload["unit"] = unit_value
        payload["unit_inferred"] = None
        payload["unit_missing"] = False
        payload["unit_source"] = "explicit"
    else:
        payload["unit"] = None
        payload["unit_inferred"] = unit_inferred
        payload["unit_missing"] = True
        payload["unit_source"] = "inferred" if unit_inferred else "missing"
    return payload


_STRICT_COMPONENTS_CAMPAIGN_CACHE: dict[int, bool] = {}


def _campaign_has_strict_components(db: Session, *, tenant_id: int, campaign_id: int) -> bool:
    campaign_row = (
        db.query(Campaign.processing_mode)
        .filter(
            Campaign.id == campaign_id,
            Campaign.tenant_id == tenant_id,
        )
        .first()
    )
    explicit_mode = _normalize_campaign_processing_mode(
        campaign_row[0] if campaign_row is not None else None
    )
    if explicit_mode == _CAMPAIGN_PROCESSING_STANDARD:
        return False
    cached = _STRICT_COMPONENTS_CAMPAIGN_CACHE.get(campaign_id)
    if cached is True:
        return True
    exists = (
        db.query(CampaignAttachment.id)
        .filter(
            CampaignAttachment.tenant_id == tenant_id,
            CampaignAttachment.campaign_id == campaign_id,
            CampaignAttachment.strict_extraction_preview.isnot(None),
        )
        .limit(1)
        .first()
        is not None
    )
    if exists:
        _STRICT_COMPONENTS_CAMPAIGN_CACHE[campaign_id] = True
    return bool(exists)


def _extract_invoice_attachment_like_components(
    item: InvoiceExtractionItem,
    *,
    db: Session,
) -> dict[str, Any]:
    base_name = re.sub(r"\s+", " ", str(item.product_name or "").strip())
    base_desc = re.sub(r"\s+", " ", str(item.description or "").strip())
    base_raw_desc = re.sub(r"\s+", " ", str(item.raw_description or "").strip())

    block_lines: list[str] = []
    if base_name:
        block_lines.append(base_name)
    if base_desc:
        for raw_line in re.split(r"[\r\n|]+", base_desc):
            line = re.sub(r"\s+", " ", str(raw_line or "").strip())
            if line:
                block_lines.append(line)
    if base_raw_desc:
        for raw_line in re.split(r"[\r\n|]+", base_raw_desc):
            line = re.sub(r"\s+", " ", str(raw_line or "").strip())
            if line:
                block_lines.append(line)
    if not block_lines:
        return {
            "raw_product_name": base_name or None,
            "raw_text": None,
            "raw_description": base_raw_desc or None,
            "characteristics": [],
            "requirements": [],
            "accessories": [],
            "description": None,
        }

    if not item.strict_components_payload and _campaign_has_strict_components(
        db,
        tenant_id=item.tenant_id,
        campaign_id=item.campaign_id,
    ):
        # Pass 1: line-only strict extraction.
        strict_payload = _extract_invoice_components_strict_with_gpt(block_lines)
        if not isinstance(strict_payload, dict):
            strict_payload = {}
        if not str(strict_payload.get("name") or "").strip() and base_name:
            strict_payload["name"] = base_name
        if isinstance(strict_payload, dict):
            strict_payload = _apply_parser_numeric_rules_to_payload(item, strict_payload)
            if strict_payload.get("vat_rate") is None and strict_payload.get("vat_rate_calc") is not None:
                strict_payload["vat_rate"] = strict_payload.get("vat_rate_calc")
                if not str(strict_payload.get("vat_source") or "").strip():
                    strict_payload["vat_source"] = "calculated_line"
            strict_payload = _ensure_unit_missing_flag(strict_payload)
        if isinstance(strict_payload, dict) and not str(strict_payload.get("name") or "").strip() and base_name:
            strict_payload["name"] = base_name
        item.strict_components_payload = strict_payload

        # Global numeric completion: one GPT pass per invoice (file + parser rows).
        if item.invoice_id is not None:
            invoice_items = (
                db.query(InvoiceExtractionItem)
                .filter(
                    InvoiceExtractionItem.invoice_id == item.invoice_id,
                    InvoiceExtractionItem.tenant_id == item.tenant_id,
                )
                .all()
            )
            completion_rows = _extract_invoice_numeric_completion_with_gpt(item.invoice, invoice_items)
            if completion_rows:
                _apply_invoice_numeric_completion(invoice_items, completion_rows)

        # Optional pass 2: one global VAT extraction per invoice (not per line).
        if item.invoice_id is not None:
            invoice_items = (
                db.query(InvoiceExtractionItem)
                .filter(
                    InvoiceExtractionItem.invoice_id == item.invoice_id,
                    InvoiceExtractionItem.tenant_id == item.tenant_id,
                )
                .all()
            )
            if invoice_items and all(
                it.strict_components_payload is not None for it in invoice_items
            ):
                if not any(
                    _strict_payload_has_vat(it.strict_components_payload)
                    for it in invoice_items
                ):
                    max_chars = max(
                        1200,
                        int(
                            getattr(
                                config,
                                "OPENAI_INVOICE_COMPONENTS_MAX_INPUT_CHARS",
                                5000,
                            )
                            or 5000
                        ),
                    )
                    global_vat = _get_invoice_global_vat(item.invoice, max_chars=max_chars)
                    if global_vat is not None:
                        for it in invoice_items:
                            payload = dict(it.strict_components_payload or {})
                            if (not str(payload.get("name") or "").strip()) and it.product_name:
                                payload["name"] = re.sub(r"\s+", " ", str(it.product_name).strip())
                            payload = _apply_parser_numeric_rules_to_payload(it, payload)
                            if payload.get("vat_rate") is None:
                                payload["vat_rate"] = global_vat.get("vat_rate")
                            if payload.get("vat_rate_calc") is None and global_vat.get("vat_rate_calc") is not None:
                                payload["vat_rate_calc"] = global_vat.get("vat_rate_calc")
                            if payload.get("vat_rate") is None and payload.get("vat_rate_calc") is not None:
                                payload["vat_rate"] = payload.get("vat_rate_calc")
                            if not str(payload.get("vat_source") or "").strip():
                                payload["vat_source"] = (
                                    str(global_vat.get("vat_source") or "").strip()
                                    or ("calculated_global" if payload.get("vat_rate_calc") is not None else None)
                                )
                            payload = _ensure_unit_missing_flag(payload)
                            it.strict_components_payload = payload

    if (
        item.invoice_id is not None
        and _campaign_has_strict_components(
            db,
            tenant_id=item.tenant_id,
            campaign_id=item.campaign_id,
        )
        and (
            not isinstance(item.strict_components_payload, dict)
            or item.strict_components_payload.get("numeric_source") != "gpt_invoice_global"
        )
    ):
        invoice_items = (
            db.query(InvoiceExtractionItem)
            .filter(
                InvoiceExtractionItem.invoice_id == item.invoice_id,
                InvoiceExtractionItem.tenant_id == item.tenant_id,
            )
            .all()
        )
        completion_rows = _extract_invoice_numeric_completion_with_gpt(item.invoice, invoice_items)
        if completion_rows:
            _apply_invoice_numeric_completion(invoice_items, completion_rows)

    if isinstance(item.strict_components_payload, dict):
        item.strict_components_payload = _apply_parser_numeric_rules_to_payload(
            item,
            item.strict_components_payload,
        )
        if (
            item.strict_components_payload.get("vat_rate") is None
            and item.strict_components_payload.get("vat_rate_calc") is not None
        ):
            item.strict_components_payload["vat_rate"] = item.strict_components_payload.get("vat_rate_calc")
            if not str(item.strict_components_payload.get("vat_source") or "").strip():
                item.strict_components_payload["vat_source"] = "calculated_fallback"
        item.strict_components_payload = _ensure_unit_missing_flag(item.strict_components_payload)
    if (
        isinstance(item.strict_components_payload, dict)
        and not str(item.strict_components_payload.get("name") or "").strip()
        and base_name
    ):
        item.strict_components_payload["name"] = base_name

    cache_key = _invoice_components_cache_key(block_lines)
    notes_payload = _extract_invoice_components_from_notes(item.notes)
    if notes_payload is not None:
        notes_payload = dict(notes_payload)
        notes_payload["raw_product_name"] = base_name or notes_payload.get("raw_product_name")
        notes_payload["raw_text"] = (
            " | ".join(_dedupe_texts(block_lines)).strip() or notes_payload.get("raw_text")
        )
        notes_payload["raw_description"] = base_raw_desc or notes_payload.get("raw_description")
        _invoice_components_cache_set(cache_key, notes_payload)
        return notes_payload

    cached_payload = _invoice_components_cache_get(cache_key)
    if cached_payload is not None:
        cached_payload = dict(cached_payload)
        cached_payload["raw_product_name"] = base_name or cached_payload.get("raw_product_name")
        cached_payload["raw_text"] = (
            " | ".join(_dedupe_texts(block_lines)).strip() or cached_payload.get("raw_text")
        )
        cached_payload["raw_description"] = base_raw_desc or cached_payload.get("raw_description")
        return cached_payload

    gpt_payload = _extract_invoice_components_with_gpt(block_lines)
    if gpt_payload is not None:
        gpt_payload = dict(gpt_payload)
        gpt_payload["raw_product_name"] = base_name or gpt_payload.get("raw_product_name")
        gpt_payload["raw_text"] = " | ".join(_dedupe_texts(block_lines)).strip() or gpt_payload.get("raw_text")
        gpt_payload["raw_description"] = base_raw_desc or gpt_payload.get("raw_description")
        _invoice_components_cache_set(cache_key, gpt_payload)
        if bool(getattr(config, "OPENAI_INVOICE_COMPONENTS_SAVE_TO_NOTES", True)):
            item.notes = _upsert_notes_b64_payload(item.notes, "inv_components_b64", gpt_payload)
        return gpt_payload

    offered_chars: list[str] = []
    try:
        from app.services.tech_sheet_extraction import parse_characteristics

        parsed = parse_characteristics("\n".join(block_lines))
        if isinstance(parsed, dict):
            offered_chars = _normalize_char_lines_with_attachment_schema(parsed.get("kv"))
    except Exception:
        offered_chars = []

    offered_accessories: list[str] = []
    offered_requirements: list[str] = []
    try:
        from app.services.campaign_attachment_extraction import (
            _extract_structured_lists_from_block,
            _normalize_accessoires_payload,
            _normalize_exigences_payload,
        )

        product_title = base_name or "Produit facture"
        accessories_raw, exigences_raw, _verify_raw = _extract_structured_lists_from_block(
            block_lines,
            product_title,
            aggressive_mode=True,
        )
        acc_rows = _normalize_accessoires_payload(accessories_raw, exigences_raw)
        req_rows = _normalize_exigences_payload(exigences_raw)
        for row in acc_rows:
            formatted = _format_accessory_entry(row)
            if formatted:
                offered_accessories.append(formatted)
        for row in req_rows:
            offered_requirements.extend(_collect_text_values(row.get("text") if isinstance(row, dict) else row))
    except Exception:
        infer_req, infer_acc = _infer_requirement_accessory_lines(block_lines)
        offered_requirements.extend(infer_req)
        offered_accessories.extend(infer_acc)

    local_payload = {
        "raw_product_name": base_name or None,
        "raw_text": " | ".join(_dedupe_texts(block_lines)).strip() or None,
        "raw_description": base_raw_desc or None,
        "characteristics": _dedupe_texts(offered_chars),
        "requirements": _dedupe_texts(offered_requirements),
        "accessories": _dedupe_texts(offered_accessories),
        "description": base_desc or None,
    }
    _invoice_components_cache_set(cache_key, local_payload)
    return local_payload


def _merge_offered_component_payloads(
    primary: dict[str, Any],
    secondary: dict[str, Any],
) -> dict[str, Any]:
    p = primary if isinstance(primary, dict) else {}
    s = secondary if isinstance(secondary, dict) else {}
    desc_values = _dedupe_texts(
        _collect_text_values(p.get("description")) + _collect_text_values(s.get("description"))
    )
    raw_desc_values = _dedupe_texts(
        _collect_text_values(p.get("raw_description")) + _collect_text_values(s.get("raw_description"))
    )
    raw_text_values = _dedupe_texts(
        _collect_text_values(p.get("raw_text")) + _collect_text_values(s.get("raw_text"))
    )
    raw_name_values = _dedupe_texts(
        _collect_text_values(p.get("raw_product_name")) + _collect_text_values(s.get("raw_product_name"))
    )
    return {
        "raw_product_name": raw_name_values[0] if raw_name_values else None,
        "raw_text": " | ".join(raw_text_values).strip() or None,
        "raw_description": " | ".join(raw_desc_values).strip() or None,
        "characteristics": _dedupe_texts(
            _collect_text_values(p.get("characteristics")) + _collect_text_values(s.get("characteristics"))
        ),
        "requirements": _dedupe_texts(
            _collect_text_values(p.get("requirements")) + _collect_text_values(s.get("requirements"))
        ),
        "accessories": _dedupe_texts(
            _collect_text_values(p.get("accessories")) + _collect_text_values(s.get("accessories"))
        ),
        "description": " | ".join(desc_values).strip() or None,
    }


def _infer_requirement_accessory_lines(lines: list[str]) -> tuple[list[str], list[str]]:
    req: list[str] = []
    acc: list[str] = []
    for raw in lines:
        text = re.sub(r"\s+", " ", str(raw or "").strip())
        if not text:
            continue
        norm = _ascii_norm(text)
        if not norm:
            continue
        if any(hint in norm for hint in _REQUIREMENT_LINE_HINTS):
            req.append(text)
        if any(hint in norm for hint in _ACCESSORY_LINE_HINTS):
            acc.append(text)
    return _dedupe_texts(req), _dedupe_texts(acc)


def _format_accessory_entry(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = re.sub(r"\s+", " ", raw.strip())
        return text or None
    if isinstance(raw, dict):
        qty = raw.get("qty")
        item = raw.get("item")
        details = raw.get("details")
        material = raw.get("material")
        group = raw.get("group")
        chunks: list[str] = []
        if qty is not None and str(qty).strip():
            chunks.append(f"{qty}x")
        if item is not None and str(item).strip():
            chunks.append(str(item).strip())
        if details is not None and str(details).strip():
            chunks.append(str(details).strip())
        if material is not None and str(material).strip():
            chunks.append(str(material).strip())
        if group is not None and str(group).strip():
            chunks.append(str(group).strip())
        text = " - ".join(chunks).strip()
        if text:
            return re.sub(r"\s+", " ", text)
    flattened = _collect_text_values(raw)
    return flattened[0] if flattened else None


def _line_similarity(left: str, right: str) -> float:
    left_norm = _ascii_norm(left)
    right_norm = _ascii_norm(right)
    if not left_norm or not right_norm:
        return 0.0
    seq = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    overlap = _overlap_ratio(_name_tokens(left_norm), _name_tokens(right_norm))
    return max(float(seq), float(overlap))


def _score_required_lines(
    required_lines: list[str],
    offered_lines: list[str],
) -> dict[str, Any]:
    required = _dedupe_texts(required_lines)
    offered = _dedupe_texts(offered_lines)
    if not required:
        return {
            "score": None,
            "matched": 0,
            "total": 0,
            "coverage": None,
            "details": [],
        }
    details: list[dict[str, Any]] = []
    matched_count = 0
    score_sum = 0.0
    for req in required:
        best_score = 0.0
        best_offer = None
        for off in offered:
            score = _line_similarity(req, off)
            if score > best_score:
                best_score = score
                best_offer = off
        score_sum += best_score
        matched = best_score >= _TECH_SIMILARITY_MATCH_THRESHOLD
        if matched:
            matched_count += 1
        details.append(
            {
                "required": req,
                "best_offered": best_offer,
                "score": round(best_score, 4),
                "matched": matched,
            }
        )
    total = len(required)
    score = score_sum / max(total, 1)
    coverage = matched_count / max(total, 1)
    return {
        "score": round(score, 4),
        "matched": int(matched_count),
        "total": int(total),
        "coverage": round(coverage, 4),
        "details": details[:20],
    }


def _flatten_requested_components(
    *,
    requested_description: str | None,
    requested_characteristics: Any,
    requested_product_name: str | None = None,
    requested_raw_product_name: str | None = None,
    requested_raw_text: str | None = None,
) -> dict[str, Any]:
    char_lines: list[str] = []
    req_lines: list[str] = []
    acc_lines: list[str] = []
    desc_lines: list[str] = []
    if requested_description:
        desc_lines.append(requested_description)

    if isinstance(requested_characteristics, dict):
        characteristic_map: dict[str, Any] = {}
        for raw_key, raw_value in requested_characteristics.items():
            key = str(raw_key or "").strip()
            key_norm = _ascii_norm(key)
            value_lines = _collect_text_values(raw_value)
            if key_norm in _ACCESSORY_KEYS or "accessoire" in key_norm:
                if value_lines:
                    acc_lines.extend(value_lines)
                continue
            if (
                key_norm in _REQUIREMENT_KEYS
                or "exig" in key_norm
                or "obligat" in key_norm
                or "must" in key_norm
            ):
                if value_lines:
                    req_lines.extend(value_lines)
                continue
            if key_norm in _DESCRIPTION_KEYS:
                desc_lines.extend(value_lines)
                continue
            characteristic_map[key] = raw_value
        char_lines.extend(_normalize_char_lines_with_attachment_schema(characteristic_map))
    else:
        char_lines.extend(_normalize_char_lines_with_attachment_schema(requested_characteristics))

    return {
        "product_name": _clean_display_text(requested_product_name) or None,
        "raw_product_name": _clean_display_text(requested_raw_product_name) or None,
        "raw_text": _clean_display_text(requested_raw_text) or None,
        "characteristics": _dedupe_texts(char_lines),
        "requirements": _dedupe_texts(req_lines),
        "accessories": _dedupe_texts(acc_lines),
        "description": " | ".join(_dedupe_texts(desc_lines)).strip() or None,
    }


def _pick_best_sheet_product_block(
    products: list[dict[str, Any]],
    target_name: str | None,
) -> dict[str, Any] | None:
    if not products:
        return None
    if len(products) == 1:
        return products[0]
    target_norm = _ascii_norm(target_name)
    if not target_norm:
        return products[0]
    target_tokens = _name_tokens(target_norm)
    best_score = -1.0
    best_entry: dict[str, Any] | None = None
    for entry in products:
        candidate = str(entry.get("name") or entry.get("product_name") or "").strip()
        cand_norm = _ascii_norm(candidate)
        seq = difflib.SequenceMatcher(None, target_norm, cand_norm).ratio() if cand_norm else 0.0
        overlap = _overlap_ratio(target_tokens, _name_tokens(cand_norm))
        score = (0.70 * seq) + (0.30 * overlap)
        if score > best_score:
            best_score = score
            best_entry = entry
    return best_entry or products[0]


def _extract_offered_components_from_sheet(
    *,
    sheet: TechSheet,
    invoice_item_name: str | None,
) -> dict[str, Any]:
    payload = sheet.characteristics if isinstance(sheet.characteristics, dict) else {}
    products_raw = payload.get("products")
    products = [row for row in products_raw if isinstance(row, dict)] if isinstance(products_raw, list) else []
    block = _pick_best_sheet_product_block(products, invoice_item_name)
    if block is None:
        block = payload if isinstance(payload, dict) else {}

    offered_chars: list[str] = []
    characteristic_map: dict[str, Any] = {}
    raw_chars = block.get("characteristics")
    if isinstance(raw_chars, list):
        for row in raw_chars:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            if not key:
                continue
            values = row.get("values")
            if values is None:
                values = row.get("value")
            characteristic_map[key] = values
    elif isinstance(raw_chars, dict):
        characteristic_map.update(raw_chars)

    # Fallback to kv map when products[] was absent.
    if isinstance(payload.get("kv"), dict):
        for raw_key, raw_value in payload.get("kv", {}).items():
            if raw_key not in characteristic_map:
                characteristic_map[str(raw_key)] = raw_value
    offered_chars = _normalize_char_lines_with_attachment_schema(characteristic_map)

    offered_accessories: list[str] = []
    raw_acc = block.get("accessoires_structures")
    if isinstance(raw_acc, list):
        for entry in raw_acc:
            formatted = _format_accessory_entry(entry)
            if formatted:
                offered_accessories.append(formatted)
    else:
        offered_accessories.extend(_collect_text_values(raw_acc))

    offered_requirements: list[str] = []
    raw_req = block.get("exigences")
    if isinstance(raw_req, list):
        for entry in raw_req:
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
                if text:
                    offered_requirements.append(text)
                    continue
            offered_requirements.extend(_collect_text_values(entry))
    else:
        offered_requirements.extend(_collect_text_values(raw_req))

    # Some legacy / noisy tech sheet payloads only expose dense technical lines
    # under characteristics. Infer requirement/accessory candidates when their
    # dedicated buckets are empty.
    if not offered_requirements or not offered_accessories:
        infer_source = list(offered_chars)
        infer_source.extend(_collect_text_values(block.get("description")))
        raw_text_value = str(sheet.raw_text or "").strip()
        if raw_text_value:
            raw_hits: list[str] = []
            for raw_line in re.split(r"[\r\n]+", raw_text_value):
                line = re.sub(r"\s+", " ", str(raw_line or "").strip())
                if not line:
                    continue
                line_norm = _ascii_norm(line)
                if not line_norm:
                    continue
                if any(hint in line_norm for hint in _REQUIREMENT_LINE_HINTS) or any(
                    hint in line_norm for hint in _ACCESSORY_LINE_HINTS
                ):
                    raw_hits.append(line)
                    if len(raw_hits) >= 120:
                        break
            infer_source.extend(raw_hits)
        infer_req, infer_acc = _infer_requirement_accessory_lines(infer_source)
        if not offered_requirements and infer_req:
            offered_requirements.extend(infer_req)
        if not offered_accessories and infer_acc:
            offered_accessories.extend(infer_acc)

    desc_chunks = _collect_text_values(block.get("description"))
    if not desc_chunks:
        desc_chunks = _collect_text_values(payload.get("description"))
    if not desc_chunks:
        desc_chunks = _collect_text_values(sheet.raw_text)[:5]

    return {
        "raw_product_name": _clean_display_text(
            str(block.get("name") or block.get("product_name") or invoice_item_name or "")
        )
        or None,
        "raw_text": _clean_display_text(str(sheet.raw_text or "")) or None,
        "raw_description": " | ".join(_dedupe_texts(desc_chunks)).strip() or None,
        "characteristics": _dedupe_texts(offered_chars),
        "requirements": _dedupe_texts(offered_requirements),
        "accessories": _dedupe_texts(offered_accessories),
        "description": " | ".join(_dedupe_texts(desc_chunks)).strip() or None,
    }


def _build_campaign_requested_index(
    *,
    db: Session,
    tenant_id: int,
    campaign_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not campaign_ids:
        return {}
    campaign_mode_rows = (
        db.query(Campaign.id, Campaign.processing_mode)
        .filter(
            Campaign.tenant_id == tenant_id,
            Campaign.id.in_(campaign_ids),
        )
        .all()
    )
    campaign_mode_by_id: dict[int, str | None] = {
        int(campaign_id): _normalize_campaign_processing_mode(processing_mode)
        for campaign_id, processing_mode in campaign_mode_rows
    }
    rows = (
        db.query(CampaignItem, Product)
        .join(Product, Product.id == CampaignItem.product_id)
        .filter(
            CampaignItem.tenant_id == tenant_id,
            CampaignItem.campaign_id.in_(campaign_ids),
            Product.tenant_id == tenant_id,
        )
        .all()
    )
    by_campaign: dict[int, list[dict[str, Any]]] = {}
    for item_row, product in rows:
        characteristics = (
            item_row.requested_characteristics
            if isinstance(item_row.requested_characteristics, dict)
            else product.characteristics
        )
        by_campaign.setdefault(item_row.campaign_id, []).append(
            {
                "product_id": item_row.product_id,
                "product_name": str(product.name or "").strip(),
                "requested_raw_product_name": None,
                "requested_raw_text": None,
                "requested_description": (
                    item_row.requested_description
                    or product.description
                    or None
                ),
                "requested_characteristics": characteristics,
            }
        )

    def _extract_strict_products(preview: Any) -> list[dict[str, Any]]:
        if not isinstance(preview, dict):
            return []
        products = preview.get("products")
        if isinstance(products, list):
            return [row for row in products if isinstance(row, dict)]
        strict_payload = preview.get("strict_payload")
        if isinstance(strict_payload, dict) and isinstance(strict_payload.get("products"), list):
            return [row for row in strict_payload.get("products") if isinstance(row, dict)]
        return []

    def _name_similarity(left: str | None, right: str | None) -> float:
        lnorm = _ascii_norm(left)
        rnorm = _ascii_norm(right)
        if not lnorm or not rnorm:
            return 0.0
        if lnorm == rnorm:
            return 1.0
        if lnorm in rnorm or rnorm in lnorm:
            return 0.97
        lt = set(_name_tokens(lnorm))
        rt = set(_name_tokens(rnorm))
        overlap = len(lt & rt) / max(len(lt | rt), 1)
        seq = difflib.SequenceMatcher(None, lnorm, rnorm).ratio()
        return max(float(seq), float(overlap))

    def _strict_row_context(strict_row: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        chunks: list[str] = []
        details: list[dict[str, str]] = []
        for key, label in (
            ("quantity", "quantite"),
            ("unit", "unite"),
            ("packaging", "conditionnement"),
            ("dimensions", "dimensions"),
            ("size", "taille"),
            ("weight", "poids"),
            ("vat_rate", "tva"),
        ):
            value = strict_row.get(key)
            if value is None:
                continue
            text = _clean_display_text(str(value))
            if not text:
                continue
            line = f"{label}: {text}"
            chunks.append(line)
            details.append({"value": line})
        context_text = " | ".join(chunks).strip() or None
        return context_text, {"strict_details": details} if details else {}

    attachments = (
        db.query(CampaignAttachment)
        .filter(
            CampaignAttachment.tenant_id == tenant_id,
            CampaignAttachment.campaign_id.in_(campaign_ids),
            CampaignAttachment.strict_extraction_preview.isnot(None),
        )
        .order_by(CampaignAttachment.campaign_id.asc(), CampaignAttachment.id.asc())
        .all()
    )
    strict_rows_by_campaign: dict[int, list[dict[str, Any]]] = {}
    for attachment in attachments:
        strict_products = _extract_strict_products(attachment.strict_extraction_preview)
        if not strict_products:
            continue
        strict_rows_by_campaign.setdefault(int(attachment.campaign_id), []).extend(strict_products)

    for campaign_id in campaign_ids:
        campaign_rows = by_campaign.get(campaign_id) or []
        strict_rows = strict_rows_by_campaign.get(campaign_id) or []
        effective_mode = campaign_mode_by_id.get(campaign_id)
        if effective_mode != _CAMPAIGN_PROCESSING_STRICT:
            if effective_mode is None and strict_rows:
                effective_mode = _CAMPAIGN_PROCESSING_STRICT
            else:
                effective_mode = _CAMPAIGN_PROCESSING_STANDARD
        if effective_mode != _CAMPAIGN_PROCESSING_STRICT:
            continue
        if not campaign_rows or not strict_rows:
            continue

        def _apply_strict_row(target: dict[str, Any], strict_row: dict[str, Any]) -> None:
            strict_name = _clean_display_text(str(strict_row.get("name") or "")) or None
            strict_raw_name = _clean_display_text(
                str(
                    strict_row.get("raw_product_name")
                    or strict_row.get("product_name")
                    or strict_row.get("name")
                    or ""
                )
            ) or None
            strict_desc, strict_char_ctx = _strict_row_context(strict_row)
            if strict_name:
                target["product_name"] = strict_name
            target["requested_raw_product_name"] = strict_raw_name
            target["requested_raw_text"] = _clean_display_text(str(strict_row.get("raw_text") or "")) or None
            # Keep details once (in requested_characteristics) to avoid duplicated
            # "requested_strict_line_used" later.
            target["requested_description"] = None
            if strict_desc and strict_char_ctx:
                target["requested_characteristics"] = strict_char_ctx
            elif strict_desc:
                target["requested_characteristics"] = {"strict_details": [{"value": strict_desc}]}
            else:
                target["requested_characteristics"] = None

        # Best deterministic mapping: if both sides have same count, keep order.
        if len(campaign_rows) == len(strict_rows):
            for idx, target in enumerate(campaign_rows):
                _apply_strict_row(target, strict_rows[idx])
            continue

        # Fallback: greedy one-to-one match between strict requested rows and campaign products.
        used_strict_indices: set[int] = set()
        for target in campaign_rows:
            base_name = str(target.get("product_name") or "").strip()
            best_idx: int | None = None
            best_score = 0.0
            for idx, strict_row in enumerate(strict_rows):
                if idx in used_strict_indices:
                    continue
                strict_name = _clean_display_text(str(strict_row.get("name") or "")) or ""
                if not strict_name:
                    continue
                score = _name_similarity(base_name, strict_name)
                if score > best_score:
                    best_score = score
                    best_idx = idx

            # Accept confident mapping. For same-count documents, allow ordered fallback.
            if best_idx is None:
                continue
            if best_score < 0.70 and len(campaign_rows) != len(strict_rows):
                continue
            used_strict_indices.add(best_idx)
            strict_row = strict_rows[best_idx]
            _apply_strict_row(target, strict_row)
    return by_campaign


def _strip_name_noise_for_match(text: str | None) -> str:
    value = _clean_display_text(text or "") or ""
    value = _ascii_norm(value)
    if not value:
        return ""

    # Drop packaging markers like BT100, B/100, P200...
    value = re.sub(
        r"\b(?:bt|b|p|boite|box|pack|paquet)\s*[/.-]?\s*\d+\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    # Drop size/dimension patterns like 7.5*7.5, 5x10, 20/25/30...
    value = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*(?:x|\*|/)\s*\d+(?:[.,]\d+)?(?:\s*(?:x|\*|/)\s*\d+(?:[.,]\d+)?)?\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    # Drop standalone measure tokens like 10CM, 200G, 5M.
    value = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*(?:cm|mm|m|kg|g|gr|mg|ml|l)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_measure_tokens_for_match(text: str) -> list[str]:
    value = _ascii_norm(text)
    if not value:
        return []
    out: list[str] = []
    # Compensable liquid format with multiplicative pack (ex: 2*10 ml -> u_20ml).
    for match in re.finditer(r"(\d+)\s*(?:x|\*)\s*(\d+(?:[.,]\d+)?)\s*([a-z]+)\b", value):
        raw_left = match.group(1).replace(",", ".")
        raw_right = match.group(2).replace(",", ".")
        raw_unit = match.group(3).lower()
        unit = _UNIT_ALIASES.get(raw_unit)
        if unit is None:
            continue
        base = _MATCH_COMPENSABLE_UNIT_TO_BASE.get(unit)
        if base is None:
            continue
        try:
            left = float(raw_left)
            right = float(raw_right)
        except Exception:
            continue
        base_unit, factor = base
        normalized = left * right * factor
        out.append(f"u_{normalized:.6g}{base_unit}")

    # Compensable liquid direct format (ex: 20 ml, 1 l).
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([a-z]+)\b", value):
        raw_num = match.group(1).replace(",", ".")
        raw_unit = match.group(2).lower()
        unit = _UNIT_ALIASES.get(raw_unit)
        if unit is None:
            continue
        base = _MATCH_COMPENSABLE_UNIT_TO_BASE.get(unit)
        if base is None:
            continue
        try:
            numeric = float(raw_num)
        except Exception:
            continue
        base_unit, factor = base
        normalized = numeric * factor
        out.append(f"u_{normalized:.6g}{base_unit}")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for token in out:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _extract_non_compensable_measure_types(values: list[str]) -> list[str]:
    found: set[str] = set()
    for raw in values:
        value = _ascii_norm(raw or "")
        if not value:
            continue
        for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([a-z]+)\b", value):
            raw_unit = match.group(2).lower()
            unit = _UNIT_ALIASES.get(raw_unit)
            if unit is None:
                continue
            if unit in _NON_COMPENSABLE_LENGTH_UNITS:
                found.add("length")
            elif unit in _NON_COMPENSABLE_WEIGHT_UNITS:
                found.add("weight")
    out: list[str] = []
    if "length" in found:
        out.append("length")
    if "weight" in found:
        out.append("weight")
    return out


def _apply_non_compensable_markers(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    probe_values = [
        str(payload.get("name") or ""),
        str(payload.get("dimensions") or ""),
        str(payload.get("size") or ""),
        str(payload.get("weight") or ""),
        str(payload.get("packaging") or ""),
        str(payload.get("unit") or ""),
        str(payload.get("unit_inferred") or ""),
    ]
    types = _extract_non_compensable_measure_types(probe_values)
    payload["has_non_compensable_measure"] = bool(types)
    payload["non_compensable_measure_types"] = types
    return payload


def _build_strict_flags_score(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    has_unit = bool(str(payload.get("unit") or "").strip())
    has_unit_inferred = bool(str(payload.get("unit_inferred") or "").strip())
    unit_missing = bool(payload.get("unit_missing"))
    has_non_comp = bool(payload.get("has_non_compensable_measure"))

    if has_unit:
        unit_score = 1.0
        unit_status = "explicit"
    elif has_unit_inferred:
        unit_score = 0.7
        unit_status = "inferred"
    elif unit_missing:
        unit_score = 0.2
        unit_status = "missing"
    else:
        unit_score = 0.5
        unit_status = "unknown"

    compensability_score = 0.0 if has_non_comp else 1.0
    score = (0.70 * unit_score) + (0.30 * compensability_score)
    score = max(0.0, min(1.0, score))

    if score >= 0.85:
        band = "high"
    elif score >= 0.60:
        band = "medium"
    else:
        band = "low"

    return {
        "strict_flags_score": round(score, 4),
        "strict_flags_score_band": band,
        "strict_flags_score_breakdown": {
            "unit_score": round(unit_score, 4),
            "unit_status": unit_status,
            "compensability_score": round(compensability_score, 4),
            "has_non_compensable_measure": has_non_comp,
            "weights": {
                "unit_score_weight": 0.70,
                "compensability_score_weight": 0.30,
            },
        },
    }


def _extract_packaging_tokens_for_match(text: str) -> list[str]:
    value = _ascii_norm(text)
    if not value:
        return []
    out: list[str] = []

    # Explicit packaging counts (BT100, B/100, boite de 100, pack 50).
    for match in re.finditer(
        r"\b(?:bt|bte|boite|box|pack|paquet|carton|sachet|b)\s*(?:de)?\s*[/]?\s*(\d{1,5})\b",
        value,
    ):
        try:
            count = int(match.group(1))
        except Exception:
            continue
        if count > 0:
            out.append(f"p_{count}")

    # Quantity + packaging size (ex: 2 boites de 100 -> p_200).
    for match in re.finditer(
        r"\b(\d{1,4})\s*(?:boites?|box|packs?|paquets?|cartons?|sachets?)\s*(?:de)?\s*(\d{1,5})\b",
        value,
    ):
        try:
            qty = int(match.group(1))
            size = int(match.group(2))
        except Exception:
            continue
        if qty > 0 and size > 0:
            out.append(f"p_{qty * size}")

    # Multiplicative packaging only when context contains packaging terms.
    for match in re.finditer(r"\b(\d{1,4})\s*(?:x|\*)\s*(\d{1,4})\b", value):
        start, end = match.span()
        context = value[max(0, start - 18) : min(len(value), end + 18)]
        if not re.search(r"\b(?:bt|bte|boite|box|pack|paquet|carton|sachet|b)\b", context):
            continue
        try:
            left = int(match.group(1))
            right = int(match.group(2))
        except Exception:
            continue
        total = left * right
        if total > 0:
            out.append(f"p_{total}")

    seen: set[str] = set()
    deduped: list[str] = []
    for token in out:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _extract_packaging_sizes_from_text(text: str) -> list[int]:
    value = _ascii_norm(text)
    if not value:
        return []
    out: list[int] = []
    for match in re.finditer(
        r"\b(?:bt|bte|boite|box|pack|paquet|carton|sachet|b)\s*(?:de)?\s*[/]?\s*(\d{1,5})\b",
        value,
    ):
        try:
            size = int(match.group(1))
        except Exception:
            continue
        if size > 0:
            out.append(size)
    # Deduplicate while preserving order.
    seen: set[int] = set()
    deduped: list[int] = []
    for n in out:
        if n in seen:
            continue
        seen.add(n)
        deduped.append(n)
    return deduped


def _extract_measure_tokens_from_values(values: list[str], *, max_items: int = 16) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "")
        tokens = _extract_measure_tokens_for_match(value) + _extract_packaging_tokens_for_match(value)
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
            if len(out) >= max_items:
                return out
    return out


def _normalize_inferred_unit(value: str | None) -> str | None:
    raw = _ascii_norm(value or "")
    raw = re.sub(r"\s+", "", raw).strip().lower()
    if not raw:
        return None
    candidate = None
    if raw in _UNIT_ALIASES:
        candidate = _UNIT_ALIASES.get(raw)
    else:
        candidate = _INFERRED_UNIT_ALIASES.get(raw)
    if not candidate:
        return None
    return candidate if candidate in _ALLOWED_INFERRED_UNITS else None


def _normalize_measure_unit(value: str | None) -> str | None:
    raw = _ascii_norm(value or "")
    raw = re.sub(r"\s+", "", raw).strip().lower()
    if not raw:
        return None
    canonical = _UNIT_ALIASES.get(raw)
    if not canonical:
        canonical = _INFERRED_UNIT_ALIASES.get(raw)
    if not canonical:
        return None
    return canonical if canonical in _ALLOWED_MEASURE_UNITS else None


def _is_packaging_unit_value(value: str | None) -> bool:
    raw = _ascii_norm(value or "")
    raw = re.sub(r"\s+", "", raw).strip().lower()
    if not raw:
        return False
    return raw in _PACKAGING_ONLY_UNITS


def _requested_match_cache_key(
    *,
    model: str,
    target_name: str,
    target_name_base: str,
    target_measure_tokens: list[str],
    target_unit_missing: bool | None,
    target_unit_inferred: str | None,
    candidates: list[dict[str, Any]],
) -> str:
    payload = {
        "version": "gpt_requested_name_measure_v3_name_score_only",
        "model": str(model or "").strip(),
        "target_name": target_name,
        "target_name_base": target_name_base,
        "target_measure_tokens": target_measure_tokens,
        "target_unit_missing": target_unit_missing,
        "target_unit_inferred": target_unit_inferred,
        "candidates": candidates,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _non_measure_tokens(values: list[str]) -> list[str]:
    return [
        token
        for token in values
        if token and not token.startswith("u_") and not token.startswith("p_")
    ]


def _build_requested_reference_context(
    *,
    item: InvoiceExtractionItem,
    matched_name: str | None,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    strict_payload = item.strict_components_payload if isinstance(item.strict_components_payload, dict) else {}
    strict_name = str(strict_payload.get("name") or "").strip()
    target_name = (
        strict_name
        or (matched_name or "").strip()
        or (item.corrected_product_name or "").strip()
        or (item.product_name or "").strip()
    )
    target_name = _clean_display_text(target_name or "") or ""
    target_name_base = _strip_name_noise_for_match(target_name)
    if not target_name:
        return None

    target_unit_missing = bool(strict_payload.get("unit_missing")) if strict_payload else None
    target_unit_inferred = str(strict_payload.get("unit_inferred") or "").strip() or None
    target_measure_tokens = _extract_measure_tokens_from_values(
        [
            target_name,
            target_name_base,
            str(strict_payload.get("dimensions") or ""),
            str(strict_payload.get("size") or ""),
            str(strict_payload.get("weight") or ""),
            str(strict_payload.get("packaging") or ""),
            str(strict_payload.get("unit") or ""),
            str(strict_payload.get("unit_inferred") or ""),
        ]
    )
    target_qty = _safe_float(strict_payload.get("quantity")) if isinstance(strict_payload, dict) else None
    if target_qty is not None and target_qty > 0:
        qty_int = int(round(target_qty))
        if abs(target_qty - qty_int) < 1e-6:
            for pack_size in _extract_packaging_sizes_from_text(str(strict_payload.get("packaging") or "")):
                token = f"p_{qty_int * pack_size}"
                if token not in target_measure_tokens:
                    target_measure_tokens.append(token)

    candidate_rows: list[dict[str, Any]] = []
    candidate_by_id: dict[int, dict[str, Any]] = {}
    for idx, row in enumerate(candidates, start=1):
        cand_name = _clean_display_text(str(row.get("product_name") or "")) or ""
        if not cand_name:
            continue
        raw_id = row.get("product_id")
        try:
            cand_id = int(raw_id) if raw_id is not None else idx
        except Exception:
            cand_id = idx
        candidate_measure_tokens = _extract_measure_tokens_from_values(
            [
                cand_name,
                str(row.get("requested_description") or ""),
                *_collect_text_values(row.get("requested_characteristics")),
            ]
        )
        candidate_payload = {
            "id": cand_id,
            "name": cand_name,
            "base_name": _strip_name_noise_for_match(cand_name),
            "measure_tokens": candidate_measure_tokens,
            "unit_missing": False if candidate_measure_tokens else True,
        }
        candidate_rows.append(candidate_payload)
        candidate_by_id[cand_id] = row
    if not candidate_rows:
        return None

    model = str(
        getattr(config, "OPENAI_PRODUCT_MATCH_MODEL", "")
        or getattr(config, "OPENAI_ATTACHMENT_MODEL", "")
        or getattr(config, "OPENAI_INVOICE_COMPONENTS_MODEL", "")
        or "gpt-5.1"
    ).strip()
    fallback_model = str(
        getattr(config, "OPENAI_PRODUCT_MATCH_FALLBACK_MODEL", "")
        or getattr(config, "OPENAI_INVOICE_COMPONENTS_FALLBACK_MODEL", "")
        or ""
    ).strip()
    threshold = max(
        0.0,
        min(
            1.0,
            float(
                getattr(config, "OPENAI_STRICT_REQUESTED_MATCH_MIN_CONFIDENCE", 0.75)
                or 0.75
            ),
        ),
    )
    timeout_s = max(
        20,
        int(getattr(config, "OPENAI_PRODUCT_MATCH_TIMEOUT_SECONDS", 45) or 45),
    )
    retry_attempts = max(
        1,
        int(getattr(config, "OPENAI_PRODUCT_MATCH_RETRY_ATTEMPTS", 3) or 3),
    )
    retry_backoff_s = max(
        0.2,
        float(getattr(config, "OPENAI_PRODUCT_MATCH_RETRY_BACKOFF_SECONDS", 1.5) or 1.5),
    )
    cache_key = _requested_match_cache_key(
        model=model,
        target_name=target_name,
        target_name_base=target_name_base,
        target_measure_tokens=target_measure_tokens,
        target_unit_missing=target_unit_missing,
        target_unit_inferred=target_unit_inferred,
        candidates=candidate_rows,
    )
    return {
        "item": item,
        "target_name": target_name,
        "target_name_base": target_name_base,
        "target_measure_tokens": target_measure_tokens,
        "target_unit_missing": target_unit_missing,
        "target_unit_inferred": target_unit_inferred,
        "candidate_rows": candidate_rows,
        "candidate_by_id": candidate_by_id,
        "model": model,
        "fallback_model": fallback_model,
        "threshold": threshold,
        "timeout_s": timeout_s,
        "retry_attempts": retry_attempts,
        "retry_backoff_s": retry_backoff_s,
        "cache_key": cache_key,
    }


def _local_requested_reference_match_from_context(
    context: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    item: InvoiceExtractionItem = context["item"]
    target_name = str(context.get("target_name") or "")
    target_name_base = str(context.get("target_name_base") or "")
    target_measure_tokens = list(context.get("target_measure_tokens") or [])
    candidate_rows = list(context.get("candidate_rows") or [])
    candidate_by_id = dict(context.get("candidate_by_id") or {})

    source_engine = str(item.source_engine or "").strip().lower()
    target_tokens = _non_measure_tokens(_name_tokens(target_name_base or target_name))
    target_token_set = set(target_tokens)
    if not target_name_base and not target_name:
        return None, None

    best_row: dict[str, Any] | None = None
    best_diag: dict[str, Any] | None = None
    second_score = -1.0
    min_token_hits = 1 if len(target_token_set) <= 2 else 2

    for candidate in candidate_rows:
        candidate_name = str(candidate.get("name") or "").strip()
        candidate_base = str(candidate.get("base_name") or "").strip()
        candidate_tokens = _non_measure_tokens(_name_tokens(candidate_base or candidate_name))
        candidate_token_set = set(candidate_tokens)
        shared_tokens = target_token_set & candidate_token_set
        target_overlap = len(shared_tokens) / len(target_token_set) if target_token_set else 0.0
        candidate_overlap = len(shared_tokens) / len(candidate_token_set) if candidate_token_set else 0.0
        char_score = difflib.SequenceMatcher(None, target_name_base, candidate_base).ratio()
        measure_overlap = _overlap_ratio(
            target_measure_tokens,
            list(candidate.get("measure_tokens") or []),
        )
        contains = bool(
            target_name_base
            and candidate_base
            and (
                target_name_base == candidate_base
                or target_name_base in candidate_base
                or candidate_base in target_name_base
            )
        )
        exact = bool(target_name_base and candidate_base and target_name_base == candidate_base)
        score = max(
            char_score,
            (0.58 * target_overlap) + (0.24 * candidate_overlap) + (0.18 * measure_overlap),
        )
        if contains and len(shared_tokens) >= min_token_hits:
            score = max(score, 0.985 if exact else 0.955)
        elif len(shared_tokens) >= min_token_hits and target_overlap >= 0.75 and char_score >= 0.90:
            score = max(score, 0.93)

        diag = {
            "best_candidate_id": candidate.get("id"),
            "confidence": round(float(score), 4),
            "reason": (
                f"local_match exact={exact} contains={contains} "
                f"shared_tokens={len(shared_tokens)} target_overlap={target_overlap:.3f} "
                f"candidate_overlap={candidate_overlap:.3f} char={char_score:.3f} "
                f"measure={measure_overlap:.3f}"
            ),
            "source": "local",
            "target_overlap": round(float(target_overlap), 4),
            "candidate_overlap": round(float(candidate_overlap), 4),
            "char_score": round(float(char_score), 4),
            "measure_overlap": round(float(measure_overlap), 4),
        }

        if best_diag is None or score > float(best_diag.get("confidence") or 0.0):
            if best_diag is not None:
                second_score = max(second_score, float(best_diag.get("confidence") or 0.0))
            best_row = candidate_by_id.get(int(candidate.get("id")))
            best_diag = diag
        else:
            second_score = max(second_score, score)

    if best_row is None or best_diag is None:
        return None, None

    best_score = float(best_diag.get("confidence") or 0.0)
    margin = best_score - max(0.0, second_score)
    target_overlap = float(best_diag.get("target_overlap") or 0.0)
    char_score = float(best_diag.get("char_score") or 0.0)
    is_xlsx = source_engine == "xlsx_parser"
    accept = False
    if best_score >= 0.995:
        accept = True
    elif is_xlsx:
        accept = (
            best_score >= 0.93
            and margin >= 0.08
            and (target_overlap >= 0.75 or char_score >= 0.93)
        )
    else:
        accept = (
            best_score >= 0.97
            and margin >= 0.12
            and (target_overlap >= 0.82 or char_score >= 0.97)
        )

    best_diag["margin"] = round(float(margin), 4)
    best_diag["decision"] = "matched" if accept else "ambiguous"
    if not accept:
        return None, best_diag
    return dict(best_row), best_diag


def _pick_requested_reference(
    *,
    item: InvoiceExtractionItem,
    matched_name: str | None,
    candidates: list[dict[str, Any]],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics["decision"] = "not_evaluated"
    if not candidates:
        if diagnostics is not None:
            diagnostics["decision"] = "no_candidates"
        return None

    context = _build_requested_reference_context(
        item=item,
        matched_name=matched_name,
        candidates=candidates,
    )
    if context is None:
        if diagnostics is not None:
            diagnostics["decision"] = "no_target_name"
        return None
    target_name = str(context["target_name"])
    target_name_base = str(context["target_name_base"])
    target_measure_tokens = list(context["target_measure_tokens"])
    target_unit_missing = context.get("target_unit_missing")
    target_unit_inferred = context.get("target_unit_inferred")
    candidate_rows = list(context["candidate_rows"])
    candidate_by_id = dict(context["candidate_by_id"])

    local_match, local_diag = _local_requested_reference_match_from_context(context)
    if diagnostics is not None and isinstance(local_diag, dict):
        diagnostics.update(local_diag)
    if isinstance(local_match, dict):
        out = dict(local_match)
        out["__gpt_match_confidence"] = float(local_diag.get("confidence") or 0.0) if isinstance(local_diag, dict) else None
        return out

    model = str(context["model"])
    fallback_model = str(context["fallback_model"])
    threshold = float(context["threshold"])
    timeout_s = int(context["timeout_s"])
    retry_attempts = int(context["retry_attempts"])
    retry_backoff_s = float(context["retry_backoff_s"])
    cache_key = str(context["cache_key"])

    if not config.OPENAI_API_KEY:
        if diagnostics is not None:
            diagnostics["decision"] = "openai_api_key_missing"
        return None
    if diagnostics is not None:
        diagnostics["threshold"] = threshold
    cached = get_openai_payload_cache(
        cache_type="requested_name_match",
        model=model,
        cache_key=cache_key,
    )
    if isinstance(cached, dict):
        try:
            cached_id = cached.get("best_candidate_id")
            cached_conf = float(cached.get("confidence"))
            cached_reason = str(cached.get("reason") or "").strip()
            if diagnostics is not None:
                diagnostics["best_candidate_id"] = cached_id
                diagnostics["confidence"] = cached_conf
                diagnostics["reason"] = cached_reason or None
                diagnostics["source"] = "cache"
            if cached_id is not None and cached_conf >= threshold:
                matched_row = candidate_by_id.get(int(cached_id))
                if matched_row is not None:
                    out = dict(matched_row)
                    inferred_unit = _normalize_inferred_unit(cached.get("inferred_unit"))
                    if inferred_unit:
                        out["__gpt_inferred_unit"] = inferred_unit
                        out["__gpt_inferred_unit_reason"] = str(cached.get("inferred_unit_reason") or "").strip() or None
                    out["__gpt_match_confidence"] = cached_conf
                    if diagnostics is not None:
                        diagnostics["decision"] = "matched"
                    return out
                if diagnostics is not None:
                    diagnostics["decision"] = "candidate_not_found"
                return None
            if diagnostics is not None:
                diagnostics["decision"] = "below_threshold" if cached_id is not None else "no_candidate"
            return None
        except Exception:
            pass

    system_prompt = (
        "Tu fais un matching strict NOM PRODUIT (offert facture -> produit demande). "
        "Utilise les synonymes et variantes orthographiques usuelles. "
        "Le score confidence (0..1) doit etre base UNIQUEMENT sur la similarite du nom nettoye "
        "(offered_base_name vs candidate.base_name). "
        "La logique unite/mesure ne doit PAS modifier le score confidence. "
        "Utilise aussi measure_tokens pour compensation strictement limitee a: "
        "liquides (1l=1000ml, 1cl=10ml) et conditionnement (BT100, B/100, 2*50). "
        "Exemples compensables: 20ml == 2*10ml ; 2 boites de 100 == 1 boite de 200. "
        "Les autres mesures (poids, dimensions, longueur) ne sont pas compensables. "
        "Si une unite manque d'un cote mais existe de l'autre, ne change pas le score; "
        "utilise cette info uniquement comme contexte de justification. "
        "Si offered_unit_missing=true et que le meilleur candidat permet de deviner une unite plausible, "
        "retourne inferred_unit (canonique: mm, cm, m, mg, g, kg, ml, cl, l). "
        "Ne deduis jamais une unite depuis le conditionnement/packaging (BT100, B/100, boite, pack). "
        "Ne retourne jamais d'unite commerciale (un, piece, pcs, rouleau, flacon). "
        "Sinon inferred_unit=null. "
        "Ignore dimensions, conditionnement, poids et quantites quand ces infos ne changent pas la famille produit. "
        "Compare surtout la famille/identite produit (ex: compresse sterile vs compresse purifiee = different). "
        "Si ambigu ou evidence faible, retourne best_candidate_id=null avec confidence basse. "
        "Retourne uniquement un JSON valide."
    )
    user_payload = {
        "offered_name": target_name,
        "offered_base_name": target_name_base,
        "offered_measure_tokens": target_measure_tokens,
        "offered_unit_missing": target_unit_missing,
        "offered_unit_inferred": target_unit_inferred,
        "candidates": candidate_rows,
    }
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "requested_name_match",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "best_candidate_id": {"type": ["integer", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                        "inferred_unit": {"type": ["string", "null"]},
                        "inferred_unit_reason": {"type": ["string", "null"]},
                    },
                    "required": [
                        "best_candidate_id",
                        "confidence",
                        "reason",
                        "inferred_unit",
                        "inferred_unit_reason",
                    ],
                },
            },
        },
    }
    parsed, used_model = call_chat_completion_json_with_retry(
        operation="requested_name_match_gpt",
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=timeout_s,
        retry_attempts=retry_attempts,
        retry_backoff_s=retry_backoff_s,
        metadata={
            "invoice_item_id": int(item.id or 0),
            "candidate_count": len(candidate_rows),
        },
    )
    if not isinstance(parsed, dict):
        if diagnostics is not None:
            diagnostics["decision"] = "gpt_parse_failed"
            diagnostics["source"] = "gpt"
        return None
    best_candidate_id = parsed.get("best_candidate_id")
    try:
        confidence = float(parsed.get("confidence"))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    inferred_unit = _normalize_inferred_unit(parsed.get("inferred_unit"))
    inferred_unit_reason = str(parsed.get("inferred_unit_reason") or "").strip() or None
    parsed_reason = str(parsed.get("reason") or "").strip()
    if diagnostics is not None:
        diagnostics["best_candidate_id"] = best_candidate_id
        diagnostics["confidence"] = confidence
        diagnostics["reason"] = parsed_reason or None
        diagnostics["source"] = "gpt"

    cache_model = str(used_model or model or "").strip() or model
    set_openai_payload_cache(
        cache_type="requested_name_match",
        model=cache_model,
        cache_key=cache_key,
        payload={
            "best_candidate_id": best_candidate_id,
            "confidence": confidence,
            "reason": parsed_reason,
            "inferred_unit": inferred_unit,
            "inferred_unit_reason": inferred_unit_reason,
            "threshold": threshold,
        },
    )

    if best_candidate_id is None or confidence < threshold:
        if diagnostics is not None:
            diagnostics["decision"] = "below_threshold" if best_candidate_id is not None else "no_candidate"
        return None
    try:
        matched_row = candidate_by_id.get(int(best_candidate_id))
        if not isinstance(matched_row, dict):
            if diagnostics is not None:
                diagnostics["decision"] = "candidate_not_found"
            return None
        out = dict(matched_row)
        if inferred_unit:
            out["__gpt_inferred_unit"] = inferred_unit
            out["__gpt_inferred_unit_reason"] = inferred_unit_reason
        out["__gpt_match_confidence"] = confidence
        if diagnostics is not None:
            diagnostics["decision"] = "matched"
        return out
    except Exception:
        if diagnostics is not None:
            diagnostics["decision"] = "candidate_not_found"
        return None


def _pick_requested_references_batch(
    *,
    items: list[InvoiceExtractionItem],
    matched_names_by_item_id: dict[int, str | None],
    candidates: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    outputs: dict[int, dict[str, Any]] = {}
    if not items or not candidates:
        return outputs

    pending_contexts: list[dict[str, Any]] = []
    shared_candidate_rows: list[dict[str, Any]] | None = None
    model = ""
    fallback_model = ""
    timeout_s = 45
    retry_attempts = 3
    retry_backoff_s = 1.5

    for item in items:
        diagnostics: dict[str, Any] = {"decision": "not_evaluated"}
        context = _build_requested_reference_context(
            item=item,
            matched_name=matched_names_by_item_id.get(item.id),
            candidates=candidates,
        )
        if context is None:
            diagnostics["decision"] = "no_target_name"
            outputs[item.id] = {"matched_row": None, "diagnostics": diagnostics}
            continue

        shared_candidate_rows = shared_candidate_rows or list(context["candidate_rows"])
        model = str(context["model"] or model)
        fallback_model = str(context["fallback_model"] or fallback_model)
        timeout_s = int(context["timeout_s"] or timeout_s)
        retry_attempts = int(context["retry_attempts"] or retry_attempts)
        retry_backoff_s = float(context["retry_backoff_s"] or retry_backoff_s)

        local_match, local_diag = _local_requested_reference_match_from_context(context)
        if isinstance(local_diag, dict):
            diagnostics.update(local_diag)
        if isinstance(local_match, dict):
            out = dict(local_match)
            out["__gpt_match_confidence"] = float(diagnostics.get("confidence") or 0.0)
            outputs[item.id] = {"matched_row": out, "diagnostics": diagnostics}
            continue

        cached = get_openai_payload_cache(
            cache_type="requested_name_match",
            model=str(context["model"]),
            cache_key=str(context["cache_key"]),
        )
        if isinstance(cached, dict):
            try:
                cached_id = cached.get("best_candidate_id")
                cached_conf = float(cached.get("confidence"))
                cached_reason = str(cached.get("reason") or "").strip()
                diagnostics["best_candidate_id"] = cached_id
                diagnostics["confidence"] = cached_conf
                diagnostics["reason"] = cached_reason or None
                diagnostics["source"] = "cache"
                diagnostics["threshold"] = float(context["threshold"])
                if cached_id is not None and cached_conf >= float(context["threshold"]):
                    matched_row = dict(context["candidate_by_id"].get(int(cached_id)) or {})
                    if matched_row:
                        inferred_unit = _normalize_inferred_unit(cached.get("inferred_unit"))
                        if inferred_unit:
                            matched_row["__gpt_inferred_unit"] = inferred_unit
                            matched_row["__gpt_inferred_unit_reason"] = (
                                str(cached.get("inferred_unit_reason") or "").strip() or None
                            )
                        matched_row["__gpt_match_confidence"] = cached_conf
                        diagnostics["decision"] = "matched"
                        outputs[item.id] = {"matched_row": matched_row, "diagnostics": diagnostics}
                        continue
            except Exception:
                pass

        pending_contexts.append(context)

    if not pending_contexts:
        return outputs
    if not config.OPENAI_API_KEY or not shared_candidate_rows:
        for context in pending_contexts:
            outputs[context["item"].id] = {
                "matched_row": None,
                "diagnostics": {
                    "decision": "openai_api_key_missing" if not config.OPENAI_API_KEY else "no_candidate",
                    "threshold": float(context["threshold"]),
                },
            }
        return outputs

    batch_size = max(
        2,
        int(getattr(config, "OPENAI_STRICT_REQUESTED_MATCH_BATCH_SIZE", 20) or 20),
    )
    system_prompt = (
        "Tu fais un matching strict NOM PRODUIT (offert facture -> produit demande) pour plusieurs lignes. "
        "Pour chaque ligne, choisis au plus un candidat. "
        "Le score confidence (0..1) doit etre base UNIQUEMENT sur la similarite du nom nettoye "
        "(offered_base_name vs candidate.base_name). "
        "La logique unite/mesure ne doit PAS modifier le score confidence. "
        "Utilise aussi measure_tokens pour compensation strictement limitee a: "
        "liquides (1l=1000ml, 1cl=10ml) et conditionnement (BT100, B/100, 2*50). "
        "Si ambigu ou evidence faible, retourne best_candidate_id=null avec confidence basse. "
        "Retourne uniquement un JSON valide."
    )

    for start in range(0, len(pending_contexts), batch_size):
        chunk = pending_contexts[start : start + batch_size]
        items_payload = [
            {
                "invoice_item_id": int(context["item"].id or 0),
                "offered_name": context["target_name"],
                "offered_base_name": context["target_name_base"],
                "offered_measure_tokens": context["target_measure_tokens"],
                "offered_unit_missing": context["target_unit_missing"],
                "offered_unit_inferred": context["target_unit_inferred"],
            }
            for context in chunk
        ]
        payload = {
            "model": model,
            "temperature": 0,
            "seed": 42,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"candidates": shared_candidate_rows, "items": items_payload},
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "requested_name_match_batch",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "results": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "invoice_item_id": {"type": "integer"},
                                        "best_candidate_id": {"type": ["integer", "null"]},
                                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                        "reason": {"type": "string"},
                                        "inferred_unit": {"type": ["string", "null"]},
                                        "inferred_unit_reason": {"type": ["string", "null"]},
                                    },
                                    "required": [
                                        "invoice_item_id",
                                        "best_candidate_id",
                                        "confidence",
                                        "reason",
                                        "inferred_unit",
                                        "inferred_unit_reason",
                                    ],
                                },
                            }
                        },
                        "required": ["results"],
                    },
                },
            },
        }
        parsed, used_model = call_chat_completion_json_with_retry(
            operation="requested_name_match_gpt_batch",
            payload=payload,
            primary_model=model,
            fallback_model=fallback_model,
            timeout_s=timeout_s,
            retry_attempts=retry_attempts,
            retry_backoff_s=retry_backoff_s,
            metadata={
                "item_count": len(chunk),
                "candidate_count": len(shared_candidate_rows),
            },
        )
        result_map: dict[int, dict[str, Any]] = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
            for raw in parsed["results"]:
                if not isinstance(raw, dict):
                    continue
                try:
                    result_map[int(raw.get("invoice_item_id"))] = raw
                except Exception:
                    continue

        cache_model = str(used_model or model or "").strip() or model
        for context in chunk:
            item = context["item"]
            diagnostics: dict[str, Any] = {
                "decision": "gpt_parse_failed",
                "threshold": float(context["threshold"]),
            }
            raw = result_map.get(int(item.id))
            if isinstance(raw, dict):
                best_candidate_id = raw.get("best_candidate_id")
                try:
                    confidence = float(raw.get("confidence"))
                except Exception:
                    confidence = 0.0
                confidence = max(0.0, min(1.0, confidence))
                reason = str(raw.get("reason") or "").strip()
                inferred_unit = _normalize_inferred_unit(raw.get("inferred_unit"))
                inferred_unit_reason = str(raw.get("inferred_unit_reason") or "").strip() or None
                diagnostics.update(
                    {
                        "best_candidate_id": best_candidate_id,
                        "confidence": confidence,
                        "reason": reason or None,
                        "source": "gpt_batch",
                        "decision": "matched"
                        if best_candidate_id is not None and confidence >= float(context["threshold"])
                        else ("below_threshold" if best_candidate_id is not None else "no_candidate"),
                    }
                )
                set_openai_payload_cache(
                    cache_type="requested_name_match",
                    model=cache_model,
                    cache_key=str(context["cache_key"]),
                    payload={
                        "best_candidate_id": best_candidate_id,
                        "confidence": confidence,
                        "reason": reason,
                        "inferred_unit": inferred_unit,
                        "inferred_unit_reason": inferred_unit_reason,
                        "threshold": float(context["threshold"]),
                    },
                )
                if best_candidate_id is not None and confidence >= float(context["threshold"]):
                    matched_row = dict(context["candidate_by_id"].get(int(best_candidate_id)) or {})
                    if matched_row:
                        if inferred_unit:
                            matched_row["__gpt_inferred_unit"] = inferred_unit
                            matched_row["__gpt_inferred_unit_reason"] = inferred_unit_reason
                        matched_row["__gpt_match_confidence"] = confidence
                        outputs[item.id] = {"matched_row": matched_row, "diagnostics": diagnostics}
                        continue
            outputs[item.id] = {"matched_row": None, "diagnostics": diagnostics}

    return outputs


def _clip_similarity_line(value: Any, *, max_len: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def _clip_similarity_lines(
    values: Any,
    *,
    max_items: int = 80,
    max_len: int = 260,
) -> list[str]:
    rows = _collect_text_values(values)
    out: list[str] = []
    for row in rows:
        text = _clip_similarity_line(row, max_len=max_len)
        if not text:
            continue
        out.append(text)
        if len(out) >= max_items:
            break
    return _dedupe_texts(out)


def _tech_similarity_cache_key(
    *,
    requested: dict[str, Any],
    offered: dict[str, Any],
) -> str:
    model = (config.OPENAI_TECH_SIMILARITY_MODEL or "").strip() or "gpt-5.1"
    mode = str(getattr(config, "OPENAI_TECH_SIMILARITY_MODE", "local") or "local").strip().lower()
    payload = {
        "mode": mode,
        "model": model,
        "prompt_version": _TECH_SIMILARITY_PROMPT_VERSION,
        "requested": requested,
        "offered": offered,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tech_similarity_cache_get(key: str) -> dict[str, Any] | None:
    now = time.time()
    row = _TECH_SIMILARITY_CACHE.get(key)
    if row:
        ts, value = row
        if (now - ts) <= _TECH_SIMILARITY_CACHE_TTL_SECONDS:
            try:
                return json.loads(json.dumps(value, ensure_ascii=False))
            except Exception:
                return value.copy() if isinstance(value, dict) else None
        _TECH_SIMILARITY_CACHE.pop(key, None)
    model = (config.OPENAI_TECH_SIMILARITY_MODEL or "").strip() or "gpt-5.1"
    cached = get_openai_payload_cache(
        cache_type="tech_similarity",
        model=model,
        cache_key=key,
    )
    if isinstance(cached, dict):
        _TECH_SIMILARITY_CACHE[key] = (time.time(), cached)
        try:
            return json.loads(json.dumps(cached, ensure_ascii=False))
        except Exception:
            return cached.copy() if isinstance(cached, dict) else None
    return None


def _tech_similarity_cache_set(key: str, value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        return
    _TECH_SIMILARITY_CACHE[key] = (time.time(), value)
    model = (config.OPENAI_TECH_SIMILARITY_MODEL or "").strip() or "gpt-5.1"
    set_openai_payload_cache(
        cache_type="tech_similarity",
        model=model,
        cache_key=key,
        payload=value,
    )
    if len(_TECH_SIMILARITY_CACHE) <= _TECH_SIMILARITY_CACHE_MAX_ENTRIES:
        return
    keep_n = int(_TECH_SIMILARITY_CACHE_MAX_ENTRIES * 0.9)
    ordered = sorted(_TECH_SIMILARITY_CACHE.items(), key=lambda kv: kv[1][0])
    newest = ordered[-keep_n:]
    _TECH_SIMILARITY_CACHE.clear()
    _TECH_SIMILARITY_CACHE.update({k: v for k, v in newest})


def _build_similarity_gpt_input(
    *,
    requested: dict[str, Any],
    offered: dict[str, Any],
    max_chars: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requested": {
            "product_name": _clip_similarity_line(
                requested.get("product_name"), max_len=320
            ),
            "raw_product_name": _clip_similarity_line(
                requested.get("raw_product_name"), max_len=320
            ),
            "raw_text": _clip_similarity_line(
                requested.get("raw_text"), max_len=5000
            ),
            "characteristics": _clip_similarity_lines(
                requested.get("characteristics"), max_items=80, max_len=260
            ),
            "requirements": _clip_similarity_lines(
                requested.get("requirements"), max_items=80, max_len=260
            ),
            "accessories": _clip_similarity_lines(
                requested.get("accessories"), max_items=80, max_len=260
            ),
            "description": _clip_similarity_line(
                requested.get("description"), max_len=4000
            ),
        },
        "offered": {
            "raw_product_name": _clip_similarity_line(
                offered.get("raw_product_name"), max_len=320
            ),
            "raw_text": _clip_similarity_line(
                offered.get("raw_text"), max_len=6500
            ),
            "raw_description": _clip_similarity_line(
                offered.get("raw_description"), max_len=3500
            ),
            "characteristics": _clip_similarity_lines(
                offered.get("characteristics"), max_items=120, max_len=260
            ),
            "requirements": _clip_similarity_lines(
                offered.get("requirements"), max_items=120, max_len=260
            ),
            "accessories": _clip_similarity_lines(
                offered.get("accessories"), max_items=120, max_len=260
            ),
            "description": _clip_similarity_line(
                offered.get("description"), max_len=5000
            ),
        },
    }
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) <= max_chars:
        return payload

    # Degrade gracefully when payload is too large.
    payload["requested"]["characteristics"] = payload["requested"]["characteristics"][:45]
    payload["requested"]["requirements"] = payload["requested"]["requirements"][:45]
    payload["requested"]["accessories"] = payload["requested"]["accessories"][:45]
    payload["offered"]["characteristics"] = payload["offered"]["characteristics"][:70]
    payload["offered"]["requirements"] = payload["offered"]["requirements"][:70]
    payload["offered"]["accessories"] = payload["offered"]["accessories"][:70]

    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) <= max_chars:
        return payload

    payload["requested"]["description"] = _clip_similarity_line(
        payload["requested"]["description"], max_len=1800
    )
    payload["requested"]["raw_text"] = _clip_similarity_line(
        payload["requested"]["raw_text"], max_len=2200
    )
    payload["offered"]["description"] = _clip_similarity_line(
        payload["offered"]["description"], max_len=2200
    )
    payload["offered"]["raw_text"] = _clip_similarity_line(
        payload["offered"]["raw_text"], max_len=2800
    )
    payload["offered"]["raw_description"] = _clip_similarity_line(
        payload["offered"]["raw_description"], max_len=1800
    )
    return payload


def _status_from_score(score: float) -> str:
    if score >= _TECH_SIMILARITY_STATUS_CONFORM_THRESHOLD:
        return "conforme"
    if score >= _TECH_SIMILARITY_STATUS_PARTIAL_THRESHOLD:
        return "partiel"
    return "faible"


def _weighted_similarity_from_components(components: dict[str, Any]) -> float | None:
    total_weight = 0.0
    weighted_score = 0.0
    for key, weight in _TECH_SIMILARITY_WEIGHTS.items():
        component = components.get(key) if isinstance(components, dict) else None
        if not isinstance(component, dict):
            continue
        score = component.get("score")
        total_raw = component.get("total")
        total_count: int | None
        try:
            total_count = int(total_raw) if total_raw is not None else None
        except Exception:
            total_count = None
        # Ignore empty components (0/0) from global conformity score.
        if total_count is not None and total_count <= 0:
            continue
        if score is None:
            continue
        try:
            score_value = float(score)
        except Exception:
            continue
        total_weight += float(weight)
        weighted_score += float(weight) * score_value

    if total_weight <= 0:
        return None
    return max(0.0, min(1.0, weighted_score / total_weight))


def _enforce_binary_component_coverage(components: dict[str, Any]) -> None:
    """Force coverage to matched/total for comparable list blocks.

    This keeps UI percentages deterministic:
    10/10 => 100%, 9/10 => 90%.
    """
    if not isinstance(components, dict):
        return
    for key in ("characteristics", "requirements", "accessories"):
        component = components.get(key)
        if not isinstance(component, dict):
            continue
        matched_raw = component.get("matched")
        total_raw = component.get("total")
        try:
            total = int(total_raw) if total_raw is not None else None
        except Exception:
            total = None
        try:
            matched = int(matched_raw) if matched_raw is not None else None
        except Exception:
            matched = None
        if total is None:
            continue
        if total <= 0:
            component["coverage"] = None
            continue
        if matched is None:
            continue
        if matched < 0:
            matched = 0
        if matched > total:
            matched = total
        component["matched"] = matched
        component["total"] = total
        component["coverage"] = round(matched / total, 4)


def _component_display_coverage(name: str, component: dict[str, Any] | None) -> float | None:
    if not isinstance(component, dict):
        return None

    def _clamp01(value: Any) -> float | None:
        if not isinstance(value, (int, float)):
            return None
        return max(0.0, min(1.0, float(value)))

    if name in {"characteristics", "requirements", "accessories"}:
        matched_raw = component.get("matched")
        total_raw = component.get("total")
        try:
            total = int(total_raw) if total_raw is not None else None
        except Exception:
            total = None
        try:
            matched = int(matched_raw) if matched_raw is not None else None
        except Exception:
            matched = None
        if total is not None:
            if total <= 0:
                return None
            if matched is None:
                return None
            if matched < 0:
                matched = 0
            if matched > total:
                matched = total
            return max(0.0, min(1.0, matched / total))
        cov = _clamp01(component.get("coverage"))
        if cov is not None:
            return cov
        return _clamp01(component.get("score"))

    if name == "description":
        score = _clamp01(component.get("score"))
        if score is not None:
            return score
        return _clamp01(component.get("coverage"))

    return None


def _display_tech_ratio_from_components(
    components: dict[str, Any] | None,
    *,
    breakdown: dict[str, Any] | None = None,
) -> float | None:
    if not isinstance(components, dict):
        return None
    offered_counts = breakdown.get("offered_counts") if isinstance(breakdown, dict) else None

    def _int_or_none(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    def _effective_weight(key: str, base_weight: float, component: dict[str, Any] | None) -> float:
        if key not in {"requirements", "accessories"}:
            return float(base_weight)
        if not isinstance(component, dict):
            return float(base_weight)
        total = _int_or_none(component.get("total"))
        matched = _int_or_none(component.get("matched"))
        if total is None or total <= 0:
            return float(base_weight)
        if matched is None or matched > 0:
            return float(base_weight)
        if not isinstance(offered_counts, dict):
            return float(base_weight)
        offered_total = _int_or_none(offered_counts.get(key))
        if offered_total is None or offered_total > 0:
            return float(base_weight)
        return float(base_weight) * float(_TECH_SIMILARITY_DISPLAY_MISSING_EVIDENCE_WEIGHT_FACTOR)

    total_weight = 0.0
    weighted_score = 0.0
    for key, weight in _TECH_SIMILARITY_WEIGHTS.items():
        coverage = _component_display_coverage(key, components.get(key))
        if coverage is None:
            continue
        effective_weight = _effective_weight(
            key,
            float(weight),
            components.get(key) if isinstance(components, dict) else None,
        )
        if effective_weight <= 0:
            continue
        total_weight += float(effective_weight)
        weighted_score += float(effective_weight) * float(coverage)
    if total_weight <= 0:
        return None
    return max(0.0, min(1.0, weighted_score / total_weight))


def _compute_tech_sheet_similarity_gpt(
    *,
    requested: dict[str, Any],
    offered: dict[str, Any],
) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEY:
        return None

    model = (config.OPENAI_TECH_SIMILARITY_MODEL or "").strip() or "gpt-5.1"
    timeout_s = max(15, int(config.OPENAI_TECH_SIMILARITY_TIMEOUT_SECONDS or 60))
    max_chars = max(2000, int(config.OPENAI_TECH_SIMILARITY_MAX_INPUT_CHARS or 30000))
    user_payload = _build_similarity_gpt_input(
        requested=requested,
        offered=offered,
        max_chars=max_chars,
    )

    system_prompt = (
        "Evaluate technical compliance between requested and offered medical products. "
        "Use the raw source fields first when they are present: requested.raw_product_name, requested.raw_text, "
        "offered.raw_product_name, offered.raw_text and offered.raw_description. "
        "Treat the structured lists as a cleaned interpretation of these source blocks, not as the only evidence. "
        "Be strict on hard constraints: numbers, units, capacities, classes, mandatory requirements and accessories. "
        "Scoring direction is mandatory: 0.0 means non-compliant, 1.0 means fully compliant. Never invert this scale. "
        "If hard constraints fail (capacity/unit/class/mandatory accessory), score must stay low (typically <= 0.35). "
        "Do not inflate score due to generic lexical similarity. "
        "When raw source text and structured fields disagree, mention the ambiguity in differences or reasoning_summary. "
        "Return only valid JSON. "
        "All human-readable output must be written in French. "
        "This applies to reason, reasoning_summary, strong_matches, differences, "
        "missing_requirements, missing_accessories, and every details entry inside component objects. "
        "Do not write these explanatory texts in English. "
        "You must provide: overall_score, overall_status, decision, comparable, reasoning_summary, "
        "strong_matches, differences, missing_requirements, missing_accessories, "
        "and component objects for characteristics, requirements, accessories, description. "
        "Each component key must be an object with: score (0..1), matched (integer), total (integer), details (array). "
        "matched/total must reflect requested lines coverage for that component."
    )
    response_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "overall_score": {"type": "number", "minimum": 0, "maximum": 1},
            "overall_status": {"type": "string"},
            "decision": {"type": "string"},
            "comparable": {"type": "boolean"},
            "reason": {"type": "string"},
            "reasoning_summary": {"type": "string"},
            "strong_matches": {"type": "array", "items": {"type": "string"}},
            "differences": {"type": "array", "items": {"type": "string"}},
            "missing_requirements": {"type": "array", "items": {"type": "string"}},
            "missing_accessories": {"type": "array", "items": {"type": "string"}},
            "characteristics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "matched": {"type": "integer", "minimum": 0},
                    "total": {"type": "integer", "minimum": 0},
                    "details": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["score", "matched", "total", "details"],
            },
            "requirements": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "matched": {"type": "integer", "minimum": 0},
                    "total": {"type": "integer", "minimum": 0},
                    "details": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["score", "matched", "total", "details"],
            },
            "accessories": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "matched": {"type": "integer", "minimum": 0},
                    "total": {"type": "integer", "minimum": 0},
                    "details": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["score", "matched", "total", "details"],
            },
            "description": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "matched": {"type": "integer", "minimum": 0},
                    "total": {"type": "integer", "minimum": 0},
                    "details": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["score", "matched", "total", "details"],
            },
        },
        "required": [
            "overall_score",
            "overall_status",
            "decision",
            "comparable",
            "reason",
            "reasoning_summary",
            "strong_matches",
            "differences",
            "missing_requirements",
            "missing_accessories",
            "characteristics",
            "requirements",
            "accessories",
            "description",
        ],
    }
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tech_similarity_compare_v2",
                "strict": True,
                "schema": response_schema,
            },
        },
    }
    parsed, _ = call_chat_completion_json_with_retry(
        operation="tech_similarity_gpt",
        payload=payload,
        primary_model=model,
        fallback_model=str(
            getattr(config, "OPENAI_TECH_SIMILARITY_FALLBACK_MODEL", "") or ""
        ).strip(),
        timeout_s=timeout_s,
        retry_attempts=max(
            1,
            int(getattr(config, "OPENAI_TECH_SIMILARITY_RETRY_ATTEMPTS", 3) or 3),
        ),
        retry_backoff_s=max(
            0.2,
            float(
                getattr(config, "OPENAI_TECH_SIMILARITY_RETRY_BACKOFF_SECONDS", 1.0)
                or 1.0
            ),
        ),
        metadata={
            "requested_characteristics": len(
                user_payload.get("requested", {}).get("characteristics") or []
            ),
            "offered_characteristics": len(
                user_payload.get("offered", {}).get("characteristics") or []
            ),
        },
    )
    if not isinstance(parsed, dict):
        return None

    def _f(value: Any) -> float:
        try:
            out = float(value)
        except Exception:
            out = 0.0
        return max(0.0, min(1.0, out))

    def _i(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(float(value))
        except Exception:
            return None

    def _requested_total_for_component(name: str) -> int:
        if name == "description":
            return 1 if str(requested.get("description") or "").strip() else 0
        rows = requested.get(name)
        if isinstance(rows, list):
            return len(rows)
        return 0

    def _list_of_strings(value: Any, *, limit: int = 24) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for row in value:
            text = str(row or "").strip()
            if not text:
                continue
            token = text.lower()
            if token in seen:
                continue
            seen.add(token)
            out.append(text[:280])
            if len(out) >= limit:
                break
        return out

    def _normalize_compare_decision(value: Any, *, score: float | None) -> str:
        raw = str(value or "").strip().lower()
        aliases = {
            "strong_match": "strong_match",
            "exact_match": "strong_match",
            "close_match": "close_match",
            "partial_match": "partial_match",
            "weak_match": "weak_match",
            "no_match": "no_match",
            "non_match": "no_match",
        }
        normalized = aliases.get(raw)
        if normalized:
            return normalized
        if score is None:
            return "weak_match"
        if score >= 0.9:
            return "strong_match"
        if score >= 0.75:
            return "close_match"
        if score >= 0.5:
            return "partial_match"
        if score >= 0.25:
            return "weak_match"
        return "no_match"

    def _parse_component(name: str) -> dict[str, Any]:
        raw_component = parsed.get(name)
        score_raw = None
        matched_raw = None
        total_raw = None
        details_raw = None
        if isinstance(raw_component, dict):
            score_raw = raw_component.get("score")
            matched_raw = raw_component.get("matched")
            total_raw = raw_component.get("total")
            details_raw = raw_component.get("details")

        # Backward compatibility with previous flat JSON keys.
        if score_raw is None:
            score_raw = parsed.get(f"{name}_score")
        if matched_raw is None:
            matched_raw = parsed.get(f"{name}_matched")
        if total_raw is None:
            total_raw = parsed.get(f"{name}_total")

        score = _f(score_raw)
        matched = _i(matched_raw)
        total = _i(total_raw)

        requested_total = _requested_total_for_component(name)
        if total is None:
            total = requested_total
        total = max(0, int(total))

        if matched is None:
            if total > 0:
                matched = int(round(score * total))
            else:
                matched = 0
        matched = max(0, int(matched))
        if total >= 0:
            matched = min(matched, total)

        coverage = (matched / total) if total > 0 else None
        return {
            "score": round(score, 4),
            "matched": int(matched),
            "total": int(total),
            "coverage": round(float(coverage), 4) if coverage is not None else None,
            "details": _list_of_strings(details_raw, limit=18),
            "source": "gpt",
        }

    overall_model = _f(parsed.get("overall_score"))
    model_status = str(parsed.get("overall_status") or "").strip().lower()
    if model_status not in {"conforme", "partiel", "faible"}:
        model_status = None
    components = {
        "characteristics": _parse_component("characteristics"),
        "requirements": _parse_component("requirements"),
        "accessories": _parse_component("accessories"),
        "description": _parse_component("description"),
    }
    overall_effective = _weighted_similarity_from_components(components)
    if overall_effective is None:
        status = "not_available"
    else:
        status = _status_from_score(overall_effective)

    decision = _normalize_compare_decision(
        parsed.get("decision"),
        score=overall_effective if overall_effective is not None else overall_model,
    )
    comparable = bool(parsed.get("comparable"))
    reasoning_summary = str(
        parsed.get("reasoning_summary") or parsed.get("reason") or ""
    ).strip()[:800]
    reason = str(parsed.get("reason") or reasoning_summary or "").strip()[:600]

    return {
        "score": round(overall_effective, 4) if overall_effective is not None else None,
        "status": status,
        "reason": reason,
        "model_status": model_status,
        "model_overall_score": round(overall_model, 4),
        "components": components,
        "model": model,
        "comparable": comparable,
        "decision": decision,
        "reasoning_summary": reasoning_summary,
        "strong_matches": _list_of_strings(parsed.get("strong_matches")),
        "differences": _list_of_strings(parsed.get("differences")),
        "missing_requirements": _list_of_strings(parsed.get("missing_requirements")),
        "missing_accessories": _list_of_strings(parsed.get("missing_accessories")),
    }


def _compute_tech_sheet_similarity(
    *,
    requested: dict[str, Any],
    offered: dict[str, Any],
) -> dict[str, Any]:
    cmp_char = _score_required_lines(
        requested.get("characteristics") or [],
        offered.get("characteristics") or [],
    )
    cmp_req = _score_required_lines(
        requested.get("requirements") or [],
        offered.get("requirements") or [],
    )
    cmp_acc = _score_required_lines(
        requested.get("accessories") or [],
        offered.get("accessories") or [],
    )
    desc_required = str(requested.get("description") or "").strip()
    desc_offered = str(offered.get("description") or "").strip()
    if desc_required:
        desc_score = _line_similarity(desc_required, desc_offered) if desc_offered else 0.0
        cmp_desc: dict[str, Any] = {
            "score": round(desc_score, 4),
            "matched": int(1 if desc_score >= _TECH_SIMILARITY_MATCH_THRESHOLD else 0),
            "total": 1,
            "coverage": round(desc_score, 4),
            "details": [
                {
                    "required": desc_required,
                    "best_offered": desc_offered or None,
                    "score": round(desc_score, 4),
                    "matched": desc_score >= _TECH_SIMILARITY_MATCH_THRESHOLD,
                }
            ],
        }
    else:
        cmp_desc = {"score": None, "matched": 0, "total": 0, "coverage": None, "details": []}

    components = {
        "characteristics": cmp_char,
        "requirements": cmp_req,
        "accessories": cmp_acc,
        "description": cmp_desc,
    }
    _enforce_binary_component_coverage(components)
    final_score = _weighted_similarity_from_components(components)
    if final_score is None:
        final_score = None
        status = "not_available"
    else:
        status = _status_from_score(final_score)

    return {
        "score": round(final_score, 4) if final_score is not None else None,
        "status": status,
        "engine": "local_text",
        "weights": _TECH_SIMILARITY_WEIGHTS,
        "match_threshold": _TECH_SIMILARITY_MATCH_THRESHOLD,
        "components": components,
        "requested_counts": {
            "characteristics": len(requested.get("characteristics") or []),
            "requirements": len(requested.get("requirements") or []),
            "accessories": len(requested.get("accessories") or []),
            "description": 1 if requested.get("description") else 0,
        },
        "offered_counts": {
            "characteristics": len(offered.get("characteristics") or []),
            "requirements": len(offered.get("requirements") or []),
            "accessories": len(offered.get("accessories") or []),
            "description": 1 if offered.get("description") else 0,
        },
    }


def _compute_tech_sheet_similarity_effective(
    *,
    requested: dict[str, Any],
    offered: dict[str, Any],
) -> dict[str, Any]:
    local_similarity = _compute_tech_sheet_similarity(
        requested=requested,
        offered=offered,
    )
    mode = str(getattr(config, "OPENAI_TECH_SIMILARITY_MODE", "local") or "local").strip().lower()
    if mode != "gpt":
        return local_similarity

    gpt_uncertain_only = bool(
        getattr(config, "OPENAI_TECH_SIMILARITY_GPT_UNCERTAIN_ONLY", True)
    )
    if gpt_uncertain_only:
        local_score_raw = local_similarity.get("score")
        try:
            local_score = float(local_score_raw) if local_score_raw is not None else None
        except Exception:
            local_score = None
        trigger_min = max(
            0.0,
            min(
                1.0,
                float(getattr(config, "OPENAI_TECH_SIMILARITY_GPT_TRIGGER_MIN", 0.45) or 0.45),
            ),
        )
        trigger_max = max(
            trigger_min,
            min(
                1.0,
                float(getattr(config, "OPENAI_TECH_SIMILARITY_GPT_TRIGGER_MAX", 0.75) or 0.75),
            ),
        )
        # No GPT when local score is clearly low/high or missing.
        if local_score is None or local_score <= trigger_min or local_score >= trigger_max:
            out = dict(local_similarity)
            out["engine"] = "local_text_guarded"
            out["gpt_skipped"] = True
            out["gpt_skip_reason"] = "local_confident_or_empty"
            return out

    cache_key = _tech_similarity_cache_key(requested=requested, offered=offered)
    cached_similarity = _tech_similarity_cache_get(cache_key)
    if isinstance(cached_similarity, dict):
        return cached_similarity

    gpt_similarity = _compute_tech_sheet_similarity_gpt(
        requested=requested,
        offered=offered,
    )
    if gpt_similarity is None:
        out = dict(local_similarity)
        out["engine"] = "local_text_fallback"
        out["gpt_fallback"] = True
        return out

    out = dict(local_similarity)
    out["engine"] = "gpt"
    out["local_score"] = local_similarity.get("score")
    out["local_status"] = local_similarity.get("status")
    out["score"] = gpt_similarity.get("score")
    out["status"] = gpt_similarity.get("status")
    out["gpt_reason"] = gpt_similarity.get("reason")
    out["gpt_model"] = gpt_similarity.get("model")
    out["gpt_model_status"] = gpt_similarity.get("model_status")
    out["gpt_compare_comparable"] = gpt_similarity.get("comparable")
    out["gpt_compare_decision"] = gpt_similarity.get("decision")
    out["gpt_compare_reasoning_summary"] = gpt_similarity.get("reasoning_summary")
    out["gpt_compare_strong_matches"] = gpt_similarity.get("strong_matches")
    out["gpt_compare_differences"] = gpt_similarity.get("differences")
    out["gpt_compare_missing_requirements"] = gpt_similarity.get("missing_requirements")
    out["gpt_compare_missing_accessories"] = gpt_similarity.get("missing_accessories")

    components = out.get("components")
    gpt_components = gpt_similarity.get("components")
    if isinstance(components, dict) and isinstance(gpt_components, dict):
        for key in ("characteristics", "requirements", "accessories", "description"):
            comp = components.get(key)
            if not isinstance(comp, dict):
                continue
            gpt_component = gpt_components.get(key)
            # New GPT payload (preferred): component object with score/matched/total.
            if isinstance(gpt_component, dict):
                gpt_score = gpt_component.get("score")
                if isinstance(gpt_score, (int, float)):
                    comp["model_score"] = round(float(gpt_score), 4)
                    comp["score"] = round(float(gpt_score), 4)
                gpt_matched = gpt_component.get("matched")
                gpt_total = gpt_component.get("total")
                gpt_coverage = gpt_component.get("coverage")
                if isinstance(gpt_matched, (int, float)):
                    comp["matched"] = int(gpt_matched)
                if isinstance(gpt_total, (int, float)):
                    comp["total"] = int(gpt_total)
                if isinstance(gpt_coverage, (int, float)):
                    comp["coverage"] = round(float(gpt_coverage), 4)
                # In GPT mode, do not mix local per-line details with GPT counts.
                comp["details"] = (
                    gpt_component.get("details")
                    if isinstance(gpt_component.get("details"), list)
                    else []
                )
                comp["source"] = "gpt"
                continue

            # Backward compatibility for legacy flat score payload.
            if isinstance(gpt_component, (int, float)):
                comp["model_score"] = round(float(gpt_component), 4)
                comp["score"] = round(float(gpt_component), 4)
                comp["source"] = "gpt"
    _enforce_binary_component_coverage(components if isinstance(components, dict) else {})
    _tech_similarity_cache_set(cache_key, out)
    return out


def _compute_tech_sheet_similarity_display(
    *,
    raw_score: float | None,
    raw_status: str | None,
    link_score: float | None,
    breakdown: dict[str, Any] | None,
    gpt_match_score: float | None = None,
) -> tuple[float | None, str]:
    status = str(raw_status or "not_available").strip() or "not_available"
    def _blend_with_gpt_match(base_score: float) -> float:
        if not isinstance(gpt_match_score, (int, float)):
            return base_score
        gpt_score = max(0.0, min(1.0, float(gpt_match_score)))
        tech_w = max(0.0, float(_TECH_SIMILARITY_DISPLAY_TECH_WEIGHT))
        gpt_w = max(0.0, float(_TECH_SIMILARITY_DISPLAY_GPT_MATCH_WEIGHT))
        denom = tech_w + gpt_w
        if denom <= 0:
            return base_score
        return ((base_score * tech_w) + (gpt_score * gpt_w)) / denom

    components = breakdown.get("components") if isinstance(breakdown, dict) else None
    tech_ratio = _display_tech_ratio_from_components(components, breakdown=breakdown)
    if tech_ratio is None and isinstance(raw_score, (int, float)):
        tech_ratio = max(0.0, min(1.0, float(raw_score)))
    if tech_ratio is None:
        return None, status
    score = float(tech_ratio)
    score = _blend_with_gpt_match(score)
    score = max(0.0, min(1.0, score))
    return round(score, 4), _status_from_score(score)


def _offer_analysis_signature(
    *,
    requested_product_name: str,
    requested_product_norm: str,
    requested_payload: dict[str, Any],
    invoice_offered_payload: dict[str, Any],
    offered_payload: dict[str, Any],
    linked_sheet_obj: TechSheet | None,
    link_score: float | None,
    gpt_match_score: float | None,
) -> str:
    payload = {
        "version": _OFFER_ANALYSIS_CACHE_VERSION,
        "requested_product_name": requested_product_name,
        "requested_product_norm": requested_product_norm,
        "requested": requested_payload,
        "invoice_offered": invoice_offered_payload,
        "offered": offered_payload,
        "tech_sheet_id": linked_sheet_obj.id if linked_sheet_obj is not None else None,
        "tech_sheet_file": linked_sheet_obj.file_name if linked_sheet_obj is not None else None,
        "link_score": round(float(link_score), 4) if isinstance(link_score, (int, float)) else None,
        "gpt_match_score": round(float(gpt_match_score), 4)
        if isinstance(gpt_match_score, (int, float))
        else None,
        "tech_mode": str(getattr(config, "OPENAI_TECH_SIMILARITY_MODE", "local") or "local"),
        "tech_model": str(getattr(config, "OPENAI_TECH_SIMILARITY_MODEL", "") or ""),
        "weights": dict(_TECH_SIMILARITY_WEIGHTS),
        "match_threshold": float(_TECH_SIMILARITY_MATCH_THRESHOLD),
        "status_thresholds": {
            "conform": float(_TECH_SIMILARITY_STATUS_CONFORM_THRESHOLD),
            "partial": float(_TECH_SIMILARITY_STATUS_PARTIAL_THRESHOLD),
        },
        "display": {
            "partial_floor": float(_TECH_SIMILARITY_DISPLAY_PARTIAL_FLOOR),
            "link_bonus": float(_TECH_SIMILARITY_DISPLAY_LINK_BONUS),
            "strong_component_coverage": float(_TECH_SIMILARITY_DISPLAY_STRONG_COMPONENT_COVERAGE),
            "tech_weight": float(_TECH_SIMILARITY_DISPLAY_TECH_WEIGHT),
            "gpt_match_weight": float(_TECH_SIMILARITY_DISPLAY_GPT_MATCH_WEIGHT),
            "missing_evidence_weight_factor": float(
                _TECH_SIMILARITY_DISPLAY_MISSING_EVIDENCE_WEIGHT_FACTOR
            ),
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compute_offer_analysis_payload(
    *,
    requested_product_name: str,
    requested_payload: dict[str, Any],
    invoice_offered_payload: dict[str, Any],
    linked_sheet_obj: TechSheet | None,
    link_score: float | None,
    gpt_match_score: float | None,
    invoice_item_name_for_sheet: str,
) -> dict[str, Any]:
    if linked_sheet_obj is None:
        similarity = _compute_tech_sheet_similarity_effective(
            requested=requested_payload,
            offered=invoice_offered_payload,
        )
        raw_score = similarity.get("score")
        raw_status = similarity.get("status")
        breakdown = {
            "requested_product_name": _clean_display_text(requested_product_name),
            "requested": requested_payload,
            "offered": invoice_offered_payload,
            "offered_sheet": None,
            "offered_invoice": invoice_offered_payload,
            "fallback_source": "invoice_only",
            **similarity,
        }
        display_score, _computed_status = _compute_tech_sheet_similarity_display(
            raw_score=raw_score,
            raw_status=raw_status,
            link_score=None,
            breakdown=breakdown,
            gpt_match_score=gpt_match_score,
        )
        return {
            "tech_sheet_similarity_score": raw_score,
            "tech_sheet_similarity_status": raw_status,
            "tech_sheet_similarity_breakdown": breakdown,
            "tech_sheet_similarity_display_score": display_score,
            "tech_sheet_similarity_display_status": "no_tech_sheet",
        }

    sheet_offered_payload = _extract_offered_components_from_sheet(
        sheet=linked_sheet_obj,
        invoice_item_name=invoice_item_name_for_sheet,
    )
    offered_payload = _merge_offered_component_payloads(
        sheet_offered_payload,
        invoice_offered_payload,
    )
    similarity = _compute_tech_sheet_similarity_effective(
        requested=requested_payload,
        offered=offered_payload,
    )
    raw_score = similarity.get("score")
    raw_status = similarity.get("status")
    breakdown = {
        "requested_product_name": _clean_display_text(requested_product_name),
        "requested": requested_payload,
        "offered": offered_payload,
        "offered_sheet": sheet_offered_payload,
        "offered_invoice": invoice_offered_payload,
        **similarity,
    }
    display_score, display_status = _compute_tech_sheet_similarity_display(
        raw_score=raw_score,
        raw_status=raw_status,
        link_score=link_score,
        breakdown=breakdown,
        gpt_match_score=gpt_match_score,
    )
    return {
        "tech_sheet_similarity_score": raw_score,
        "tech_sheet_similarity_status": raw_status,
        "tech_sheet_similarity_breakdown": breakdown,
        "tech_sheet_similarity_display_score": display_score,
        "tech_sheet_similarity_display_status": display_status,
    }


def _extract_offer_component_summary(payload: dict[str, Any]) -> dict[str, Any]:
    breakdown = payload.get("tech_sheet_similarity_breakdown")
    components = breakdown.get("components") if isinstance(breakdown, dict) else None
    out: dict[str, Any] = {
        "characteristics_matched": None,
        "characteristics_total": None,
        "characteristics_coverage": None,
        "requirements_matched": None,
        "requirements_total": None,
        "requirements_coverage": None,
        "accessories_matched": None,
        "accessories_total": None,
        "accessories_coverage": None,
        "description_score": None,
    }
    if not isinstance(components, dict):
        return out

    for source_key, prefix in (
        ("characteristics", "characteristics"),
        ("requirements", "requirements"),
        ("accessories", "accessories"),
    ):
        component = components.get(source_key)
        if not isinstance(component, dict):
            continue
        matched = component.get("matched")
        total = component.get("total")
        coverage = component.get("coverage")
        if isinstance(matched, (int, float)):
            out[f"{prefix}_matched"] = int(matched)
        if isinstance(total, (int, float)):
            out[f"{prefix}_total"] = int(total)
        if isinstance(coverage, (int, float)):
            out[f"{prefix}_coverage"] = float(coverage)

    description = components.get("description")
    if isinstance(description, dict):
        score = description.get("score")
        if isinstance(score, (int, float)):
            out["description_score"] = float(score)
    return out


def _upsert_offer_analysis_result(
    *,
    db: Session,
    item: InvoiceExtractionItem,
    requested_product_name: str,
    requested_product_norm: str,
    requested_product_id: int | None,
    linked_sheet_obj: TechSheet | None,
    signature: str,
    payload: dict[str, Any],
    gpt_match_score: float | None,
) -> None:
    requested_norm_key = requested_product_norm[:255]
    row = (
        db.query(OfferAnalysisResult)
        .filter(
            OfferAnalysisResult.tenant_id == item.tenant_id,
            OfferAnalysisResult.campaign_id == item.campaign_id,
            OfferAnalysisResult.invoice_item_id == item.id,
            OfferAnalysisResult.requested_product_norm == requested_norm_key,
        )
        .first()
    )
    if row is None:
        for pending in db.new:
            if not isinstance(pending, OfferAnalysisResult):
                continue
            if int(getattr(pending, "tenant_id", 0) or 0) != int(item.tenant_id or 0):
                continue
            if int(getattr(pending, "campaign_id", 0) or 0) != int(item.campaign_id or 0):
                continue
            if int(getattr(pending, "invoice_item_id", 0) or 0) != int(item.id or 0):
                continue
            if str(getattr(pending, "requested_product_norm", "") or "") != requested_norm_key:
                continue
            row = pending
            break
    if row is None:
        row = OfferAnalysisResult(
            tenant_id=item.tenant_id,
            campaign_id=item.campaign_id,
            invoice_item_id=item.id,
            requested_product_name=requested_product_name[:255],
            requested_product_norm=requested_norm_key,
        )
        db.add(row)

    summary = _extract_offer_component_summary(payload)
    row.requested_product_id = requested_product_id
    row.requested_product_name = requested_product_name[:255]
    row.requested_product_norm = requested_norm_key
    row.tech_sheet_id = linked_sheet_obj.id if linked_sheet_obj is not None else None
    row.signature = signature
    row.gpt_match_score = gpt_match_score
    row.tech_sheet_similarity_score = payload.get("tech_sheet_similarity_score")
    row.tech_sheet_similarity_status = payload.get("tech_sheet_similarity_status")
    row.tech_sheet_similarity_display_score = payload.get("tech_sheet_similarity_display_score")
    row.tech_sheet_similarity_display_status = payload.get("tech_sheet_similarity_display_status")
    row.characteristics_matched = summary.get("characteristics_matched")
    row.characteristics_total = summary.get("characteristics_total")
    row.characteristics_coverage = summary.get("characteristics_coverage")
    row.requirements_matched = summary.get("requirements_matched")
    row.requirements_total = summary.get("requirements_total")
    row.requirements_coverage = summary.get("requirements_coverage")
    row.accessories_matched = summary.get("accessories_matched")
    row.accessories_total = summary.get("accessories_total")
    row.accessories_coverage = summary.get("accessories_coverage")
    row.description_score = summary.get("description_score")
    row.payload = payload


def invalidate_offer_analysis_results(
    *,
    db: Session,
    tenant_id: int,
    campaign_id: int | None = None,
    invoice_id: int | None = None,
    item_ids: list[int] | None = None,
) -> int:
    query = db.query(OfferAnalysisResult).filter(OfferAnalysisResult.tenant_id == tenant_id)
    if campaign_id is not None:
        query = query.filter(OfferAnalysisResult.campaign_id == campaign_id)
    scoped_item_ids = [int(item_id) for item_id in (item_ids or []) if item_id is not None]
    if invoice_id is not None:
        invoice_item_ids = [
            row[0]
            for row in (
                db.query(InvoiceExtractionItem.id)
                .filter(
                    InvoiceExtractionItem.tenant_id == tenant_id,
                    InvoiceExtractionItem.invoice_id == invoice_id,
                )
                .all()
            )
        ]
        scoped_item_ids.extend(invoice_item_ids)
    if scoped_item_ids:
        query = query.filter(OfferAnalysisResult.invoice_item_id.in_(sorted(set(scoped_item_ids))))
    return int(query.delete(synchronize_session=False) or 0)


def _get_or_compute_offer_analysis(
    *,
    db: Session,
    item: InvoiceExtractionItem,
    requested_product_name: str,
    requested_product_norm: str,
    requested_product_id: int | None = None,
    requested_payload: dict[str, Any],
    linked_sheet_obj: TechSheet | None,
    link_score: float | None,
    gpt_match_score: float | None,
    invoice_item_name_for_sheet: str,
) -> tuple[dict[str, Any], bool]:
    invoice_offered_payload = _extract_invoice_attachment_like_components(item, db=db)
    if linked_sheet_obj is None:
        offered_payload = invoice_offered_payload
    else:
        offered_payload = _merge_offered_component_payloads(
            _extract_offered_components_from_sheet(
                sheet=linked_sheet_obj,
                invoice_item_name=invoice_item_name_for_sheet,
            ),
            invoice_offered_payload,
        )

    requested_norm_key = requested_product_norm[:255]
    signature = _offer_analysis_signature(
        requested_product_name=requested_product_name,
        requested_product_norm=requested_product_norm,
        requested_payload=requested_payload,
        invoice_offered_payload=invoice_offered_payload,
        offered_payload=offered_payload,
        linked_sheet_obj=linked_sheet_obj,
        link_score=link_score,
        gpt_match_score=gpt_match_score,
    )
    row = (
        db.query(InvoiceItemOfferAnalysisCache)
        .filter(
            InvoiceItemOfferAnalysisCache.tenant_id == item.tenant_id,
            InvoiceItemOfferAnalysisCache.campaign_id == item.campaign_id,
            InvoiceItemOfferAnalysisCache.invoice_item_id == item.id,
            InvoiceItemOfferAnalysisCache.requested_product_norm == requested_norm_key,
        )
        .first()
    )
    if row is None:
        for pending in db.new:
            if not isinstance(pending, InvoiceItemOfferAnalysisCache):
                continue
            if int(getattr(pending, "tenant_id", 0) or 0) != int(item.tenant_id or 0):
                continue
            if int(getattr(pending, "campaign_id", 0) or 0) != int(item.campaign_id or 0):
                continue
            if int(getattr(pending, "invoice_item_id", 0) or 0) != int(item.id or 0):
                continue
            if str(getattr(pending, "requested_product_norm", "") or "") != requested_norm_key:
                continue
            row = pending
            break
    if row is not None and row.signature == signature and isinstance(row.payload, dict):
        materialized = (
            db.query(OfferAnalysisResult)
            .filter(
                OfferAnalysisResult.tenant_id == item.tenant_id,
                OfferAnalysisResult.campaign_id == item.campaign_id,
                OfferAnalysisResult.invoice_item_id == item.id,
                OfferAnalysisResult.requested_product_norm == requested_norm_key,
            )
            .first()
        )
        if materialized is None or materialized.signature != signature:
            _upsert_offer_analysis_result(
                db=db,
                item=item,
                requested_product_name=requested_product_name,
                requested_product_norm=requested_product_norm,
                requested_product_id=requested_product_id,
                linked_sheet_obj=linked_sheet_obj,
                signature=signature,
                payload=dict(row.payload),
                gpt_match_score=gpt_match_score,
            )
            return dict(row.payload), True
        return dict(row.payload), False

    payload = _compute_offer_analysis_payload(
        requested_product_name=requested_product_name,
        requested_payload=requested_payload,
        invoice_offered_payload=invoice_offered_payload,
        linked_sheet_obj=linked_sheet_obj,
        link_score=link_score,
        gpt_match_score=gpt_match_score,
        invoice_item_name_for_sheet=invoice_item_name_for_sheet,
    )
    if row is None:
        row = InvoiceItemOfferAnalysisCache(
            tenant_id=item.tenant_id,
            campaign_id=item.campaign_id,
            invoice_item_id=item.id,
            requested_product_name=requested_product_name[:255],
            requested_product_norm=requested_norm_key,
        )
        db.add(row)
    row.requested_product_name = requested_product_name[:255]
    row.requested_product_norm = requested_norm_key
    row.tech_sheet_id = linked_sheet_obj.id if linked_sheet_obj is not None else None
    row.signature = signature
    row.payload = payload
    _upsert_offer_analysis_result(
        db=db,
        item=item,
        requested_product_name=requested_product_name,
        requested_product_norm=requested_product_norm,
        requested_product_id=requested_product_id,
        linked_sheet_obj=linked_sheet_obj,
        signature=signature,
        payload=payload,
        gpt_match_score=gpt_match_score,
    )
    return payload, True


def _canonical_token(token: str) -> str:
    value = token.strip().lower()
    if not value:
        return value
    value = _TOKEN_SYNONYMS.get(value, value)
    if value.endswith("s") and len(value) > 4:
        singular = value[:-1]
        value = _TOKEN_SYNONYMS.get(singular, singular)
    return value


def _extract_unit_tokens(text: str) -> list[str]:
    output: list[str] = []
    if not text:
        return output
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([a-z]+)\b", text):
        raw_num = match.group(1).replace(",", ".")
        raw_unit = match.group(2).lower()
        unit = _UNIT_ALIASES.get(raw_unit)
        if unit is None:
            continue
        try:
            value = float(raw_num)
        except Exception:
            continue
        base = _UNIT_TO_BASE.get(unit)
        if base is None:
            continue
        base_unit, factor = base
        normalized = value * factor
        output.append(f"u_{normalized:.6g}{base_unit}")
    return output


def _name_tokens(text: str | None) -> list[str]:
    value_raw = _norm(text)
    if not value_raw:
        return []
    value = (
        unicodedata.normalize("NFKD", value_raw)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    unit_tokens = _extract_unit_tokens(value)
    raw_tokens = [token for token in re.split(r"[^a-z0-9]+", value) if token]
    norm_tokens: list[str] = []
    for token in raw_tokens:
        if token in _NAME_STOPWORDS:
            continue
        canon = _canonical_token(token)
        if not canon or canon in _NAME_STOPWORDS:
            continue
        norm_tokens.append(canon)
    return norm_tokens + unit_tokens


def _overlap_ratio(required_tokens: list[str], offered_tokens: list[str]) -> float:
    req = [token for token in dict.fromkeys(required_tokens) if token]
    offered = set(token for token in offered_tokens if token)
    if not req:
        return 0.0
    matched = sum(1 for token in req if token in offered)
    return matched / len(req)


def _extract_similarity_points(
    similarity_points: dict | None,
) -> tuple[int | None, int | None]:
    if not isinstance(similarity_points, dict):
        return None, None
    matched = similarity_points.get("matched_points")
    total = similarity_points.get("total_points")
    matched_int: int | None = None
    total_int: int | None = None
    try:
        matched_int = int(matched) if matched is not None else None
    except Exception:
        matched_int = None
    try:
        total_int = int(total) if total is not None else None
    except Exception:
        total_int = None
    return matched_int, total_int


def _compute_emb_final_for_item(
    item: InvoiceExtractionItem,
    matched_name: str | None,
) -> tuple[float | None, int | None, int | None]:
    emb_raw = _extract_emb_score_from_notes(item.notes)
    matched_points, total_points = _extract_similarity_points(item.similarity_points)
    matched_name_clean = (matched_name or "").strip()
    if matched_name_clean:
        fuzzy_score = difflib.SequenceMatcher(
            None,
            _norm(f"{item.product_name or ''} {item.description or ''}"),
            _norm(matched_name_clean),
        ).ratio()
        if fuzzy_score >= 0.85:
            return round(float(fuzzy_score), 4), matched_points, total_points

    if emb_raw is None:
        return None, matched_points, total_points
    if not matched_name_clean:
        emb_final = float(emb_raw)
        if isinstance(matched_points, int) and matched_points > 0:
            emb_final += 0.015 * matched_points
        emb_final = max(0.0, min(1.0, emb_final))
        return round(emb_final, 4), matched_points, total_points

    emb_final = float(emb_raw)
    # Keep the historical behavior for display score:
    # emb_final is based on embedding score, with optional small bonus from
    # matched characteristics. No overlap/accessory penalties are applied here.
    if isinstance(matched_points, int) and matched_points > 0:
        emb_final += 0.015 * matched_points
    emb_final = max(0.0, min(1.0, emb_final))
    return round(emb_final, 4), matched_points, total_points


def _is_non_product_line(name: str | None, description: str | None) -> bool:
    text = f"{name or ''} {description or ''}".strip().lower()
    if not text:
        return True

    phrase_terms = {
        "bon de",
        "facture",
        "fournisseur",
        "nif",
    }
    if any(term in text for term in phrase_terms):
        return True

    # Match short markers only as full tokens to avoid false positives
    # (ex: "ai" in "air", "ht" inside regular words).
    tokens = set(re.findall(r"[a-z0-9]+", text))
    header_tokens = {
        "rc",
        "nis",
        "ai",
        "adresse",
        "tel",
        "fax",
        "rib",
        "banque",
        "client",
        "date",
        "tva",
        "ttc",
        "ht",
        "total",
        "montant",
        "objet",
        "sarl",
        "spa",
        "eurl",
        "bp",
    }
    return any(term in tokens for term in header_tokens)

@router.get("/items", response_model=list[InvoiceExtractionItemOut])
def list_items(
    status: ExtractionStatus | None = None,
    campaign_id: int | None = None,
    invoice_id: int | None = None,
    low_reliability_only: bool = False,
    low_reliability_threshold: float = 0.85,
    products_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[InvoiceExtractionItemOut]:
    base_query = db.query(InvoiceExtractionItem).filter(
        InvoiceExtractionItem.tenant_id == current_user.tenant_id
    )
    base_query = base_query.filter(
        (InvoiceExtractionItem.notes.is_(None)) | (InvoiceExtractionItem.notes != "admin_line")
    )
    query = base_query
    # Hide rejected (emb_reject) items from verification/results listings,
    # except when low-reliability filter is active (we want to see all lines).
    if not low_reliability_only:
        query = query.filter(InvoiceExtractionItem.status != ExtractionStatus.PENDING)
    if status is not None:
        query = query.filter(InvoiceExtractionItem.status == status)
    if campaign_id is not None:
        query = query.filter(InvoiceExtractionItem.campaign_id == campaign_id)
        base_query = base_query.filter(InvoiceExtractionItem.campaign_id == campaign_id)
    if invoice_id is not None:
        query = query.filter(InvoiceExtractionItem.invoice_id == invoice_id)
        base_query = base_query.filter(InvoiceExtractionItem.invoice_id == invoice_id)
    items = query.order_by(InvoiceExtractionItem.created_at.desc()).all()
    if low_reliability_only:
        # Compute per-invoice reliability: 1 - (unknown/items)
        by_invoice: dict[int, list[InvoiceExtractionItem]] = {}
        # Use all items (including rejected) for reliability calculation.
        for item in base_query.order_by(InvoiceExtractionItem.created_at.desc()).all():
            if item.invoice_id is None:
                continue
            by_invoice.setdefault(item.invoice_id, []).append(item)
        low_invoice_ids: set[int] = set()
        for invoice_id, inv_items in by_invoice.items():
            if not inv_items:
                continue
            # If everything is confirmed, invoice is considered 100% reliable.
            if all(it.status == ExtractionStatus.CONFIRMED for it in inv_items):
                continue
            unknown = sum(
                1
                for it in inv_items
                if (it.product_name or "").strip().upper() == "UNKNOWN"
            )
            score = 1 - (unknown / len(inv_items))
            if score <= low_reliability_threshold:
                low_invoice_ids.add(invoice_id)
        items = [it for it in items if it.invoice_id in low_invoice_ids]
    if products_only and not low_reliability_only:
        filtered: list[InvoiceExtractionItem] = []
        for it in items:
            name = (it.product_name or "").strip()
            if name.upper() == "UNKNOWN":
                continue
            if _is_non_product_line(it.product_name, it.description):
                continue
            filtered.append(it)
        items = filtered
    item_ids = [item.id for item in items]
    tech_link_by_item_id: dict[int, tuple[TechSheetLink, TechSheet]] = {}
    if item_ids:
        links = (
            db.query(TechSheetLink, TechSheet)
            .join(TechSheet, TechSheetLink.tech_sheet_id == TechSheet.id)
            .filter(
                TechSheetLink.tenant_id == current_user.tenant_id,
                TechSheetLink.invoice_item_id.in_(item_ids),
                TechSheet.tenant_id == current_user.tenant_id,
            )
            .all()
        )
        for link, sheet in links:
            tech_link_by_item_id[link.invoice_item_id] = (link, sheet)

    campaign_ids = sorted({item.campaign_id for item in items if item.campaign_id is not None})
    requested_index_by_campaign = _build_campaign_requested_index(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_ids=campaign_ids,
    )

    result: list[InvoiceExtractionItemOut] = []
    analysis_cache_dirty = False
    for item in items:
        supplier_name = None
        supplier_email = None
        invoice_file_name = None
        invoice_file_path = None
        if item.invoice is not None:
            supplier_email = item.invoice.sender_email
            supplier_name = item.invoice.sender_name or item.invoice.sender_email
            invoice_file_name = item.invoice.file_name
            invoice_file_path = item.invoice.file_path
        linked_sheet = tech_link_by_item_id.get(item.id)
        linked_sheet_obj = linked_sheet[1] if linked_sheet is not None else None
        matched_name = (
            (item.corrected_product_name or "").strip()
            or _extract_match_name_from_notes(item.notes)
        )
        emb_final, matched_points, total_points = _compute_emb_final_for_item(
            item, matched_name
        )
        payload = InvoiceExtractionItemOut.from_orm(item).dict()
        payload["product_name"] = _clean_display_text(payload.get("product_name"))
        payload["description"] = _clean_display_text(payload.get("description"))
        payload["corrected_product_name"] = _clean_display_text(payload.get("corrected_product_name"))
        payload["supplier_name"] = _clean_display_text(supplier_name)
        payload["supplier_email"] = supplier_email
        payload["invoice_file_name"] = _clean_display_text(invoice_file_name)
        payload["invoice_file_path"] = invoice_file_path
        payload["tech_sheet_id"] = linked_sheet_obj.id if linked_sheet_obj is not None else None
        payload["tech_sheet_file"] = (
            _clean_display_text(linked_sheet_obj.file_name)
            if linked_sheet_obj is not None
            else None
        )
        payload["tech_sheet_file_path"] = (
            linked_sheet_obj.file_path if linked_sheet_obj is not None else None
        )
        payload["matched_product_name"] = _clean_display_text(matched_name)
        payload["embedding_match_rate"] = emb_final
        gpt_match_rate = _extract_gpt_score_from_notes(item.notes)
        payload["matched_characteristics"] = matched_points
        payload["total_characteristics"] = total_points

        requested_candidates = requested_index_by_campaign.get(item.campaign_id, [])
        requested_match_diag: dict[str, Any] = {}
        requested_ref = _pick_requested_reference(
            item=item,
            matched_name=matched_name,
            candidates=requested_candidates,
            diagnostics=requested_match_diag,
        )
        req_conf = _safe_float(requested_match_diag.get("confidence"))
        req_threshold = _safe_float(requested_match_diag.get("threshold"))
        req_decision = str(requested_match_diag.get("decision") or "").strip() or None
        req_reason = str(requested_match_diag.get("reason") or "").strip() or None
        req_best_id = requested_match_diag.get("best_candidate_id")
        try:
            req_best_id = int(req_best_id) if req_best_id is not None else None
        except Exception:
            req_best_id = None
        if req_conf is None:
            req_band = None
        elif req_conf >= 0.85:
            req_band = "high"
        elif req_conf >= 0.60:
            req_band = "medium"
        else:
            req_band = "low"
        payload["requested_match_gpt_confidence"] = req_conf
        payload["requested_match_gpt_threshold"] = req_threshold
        payload["requested_match_gpt_decision"] = req_decision
        payload["requested_match_gpt_reason"] = req_reason
        payload["requested_match_gpt_best_candidate_id"] = req_best_id
        payload["requested_match_gpt_score_band"] = req_band
        if requested_ref is None:
            payload["tech_sheet_similarity_score"] = None
            payload["tech_sheet_similarity_status"] = "no_requested_reference"
            payload["tech_sheet_similarity_breakdown"] = None
            payload["tech_sheet_similarity_display_score"] = None
            payload["tech_sheet_similarity_display_status"] = "no_requested_reference"
        else:
            requested_payload = _flatten_requested_components(
                requested_product_name=requested_ref.get("product_name"),
                requested_raw_product_name=requested_ref.get("requested_raw_product_name"),
                requested_raw_text=requested_ref.get("requested_raw_text"),
                requested_description=requested_ref.get("requested_description"),
                requested_characteristics=requested_ref.get("requested_characteristics"),
            )
            link_score = None
            if linked_sheet is not None and linked_sheet[0] is not None:
                try:
                    link_score = float(linked_sheet[0].score)
                except Exception:
                    link_score = None
            analysis_payload, analysis_dirty = _get_or_compute_offer_analysis(
                db=db,
                item=item,
                requested_product_name=str(requested_ref.get("product_name") or ""),
                requested_product_norm=_norm(str(requested_ref.get("product_name") or "")),
                requested_product_id=int(requested_ref.get("product_id") or 0) or None,
                requested_payload=requested_payload,
                linked_sheet_obj=linked_sheet_obj,
                link_score=link_score,
                gpt_match_score=gpt_match_rate,
                invoice_item_name_for_sheet=item.product_name or matched_name,
            )
            analysis_cache_dirty = analysis_cache_dirty or analysis_dirty
            payload.update(analysis_payload)

        result.append(payload)
    if analysis_cache_dirty:
        db.commit()
    return result


@router.get("/items/{item_id}/characteristics-match", response_model=dict)
def get_item_characteristics_match(
    item_id: int,
    requested_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    item = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.id == item_id,
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    thresholds = get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)
    char_threshold = float(thresholds.characteristics_threshold)
    requested_norm = _norm(requested_name)

    pair_row = None
    if requested_norm:
        pair_row = (
            db.query(InvoiceItemCharacteristicMatch)
            .filter(
                InvoiceItemCharacteristicMatch.tenant_id == current_user.tenant_id,
                InvoiceItemCharacteristicMatch.campaign_id == item.campaign_id,
                InvoiceItemCharacteristicMatch.invoice_item_id == item.id,
                InvoiceItemCharacteristicMatch.requested_product_norm == requested_norm,
            )
            .first()
        )
    if pair_row is None:
        candidates = (
            db.query(InvoiceItemCharacteristicMatch)
            .filter(
                InvoiceItemCharacteristicMatch.tenant_id == current_user.tenant_id,
                InvoiceItemCharacteristicMatch.campaign_id == item.campaign_id,
                InvoiceItemCharacteristicMatch.invoice_item_id == item.id,
            )
            .all()
        )
        best_ratio = 0.0
        for row in candidates:
            ratio = difflib.SequenceMatcher(
                None,
                requested_norm,
                _norm(row.requested_product_norm),
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                pair_row = row
        if best_ratio < 0.95:
            pair_row = None

    requested_rows = _collect_campaign_requested_characteristics(
        db=db,
        tenant_id=item.tenant_id,
        campaign_id=item.campaign_id,
    )
    requested = _select_requested_characteristics_for_product(requested_name, requested_rows)

    # Repair stale cache on demand: if requested characteristics exist but this pair
    # is missing or still 0/0, rebuild cache for this item before returning details.
    refresh_pair_cache = False
    if requested:
        if pair_row is None:
            refresh_pair_cache = True
        else:
            try:
                total_points_raw = (
                    int(pair_row.total_points)
                    if pair_row.total_points is not None
                    else 0
                )
            except Exception:
                total_points_raw = 0
            points_empty = not isinstance(pair_row.points, list) or len(pair_row.points) == 0
            if total_points_raw <= 0 and points_empty:
                refresh_pair_cache = True
    if refresh_pair_cache and item.id is not None:
        try:
            rebuild_characteristics_pair_cache(
                db=db,
                tenant_id=item.tenant_id,
                campaign_id=item.campaign_id,
                threshold=char_threshold,
                item_ids=[item.id],
            )
            db.flush()
            if requested_norm:
                pair_row = (
                    db.query(InvoiceItemCharacteristicMatch)
                    .filter(
                        InvoiceItemCharacteristicMatch.tenant_id == current_user.tenant_id,
                        InvoiceItemCharacteristicMatch.campaign_id == item.campaign_id,
                        InvoiceItemCharacteristicMatch.invoice_item_id == item.id,
                        InvoiceItemCharacteristicMatch.requested_product_norm == requested_norm,
                    )
                    .first()
                )
        except Exception as exc:
            logger.warning(
                "Characteristic pair-cache refresh failed: tenant=%s campaign=%s item=%s err=%s",
                item.tenant_id,
                item.campaign_id,
                item.id,
                exc,
            )
            db.rollback()

    if pair_row is not None:
        points = pair_row.points if isinstance(pair_row.points, list) else []
        matched_points = pair_row.matched_points if pair_row.matched_points is not None else 0
        total_points = pair_row.total_points if pair_row.total_points is not None else 0
        return {
            "item_id": item.id,
            "requested_name": _clean_display_text(requested_name),
            "item_product_name": _clean_display_text(item.product_name),
            "matched_product_name": _clean_display_text(pair_row.matched_product_name),
            "characteristics_threshold": float(pair_row.threshold or char_threshold),
            "score": round(float(pair_row.score or 0.0), 4),
            "matched_points": int(matched_points),
            "total_points": int(total_points),
            "points": points,
            "requested_characteristics": requested,
            "source": pair_row.source or "pair_cache",
        }

    # Last-resort on-demand computation for this requested product so the
    # dialog does not stay empty when cache lookup misses.
    if requested:
        offered, offered_source = _extract_offered_characteristics(
            db=db,
            item=item,
            tenant_id=item.tenant_id,
            campaign_id=item.campaign_id,
        )
        score, details = _compare_characteristics_with_embeddings(
            requested=requested,
            offered=offered,
            threshold=char_threshold,
        )
        points = details.get("points") if isinstance(details, dict) and isinstance(details.get("points"), list) else []
        matched_points = (
            int(details.get("matched_points"))
            if isinstance(details, dict) and details.get("matched_points") is not None
            else 0
        )
        total_points = (
            int(details.get("total_points"))
            if isinstance(details, dict) and details.get("total_points") is not None
            else len(requested)
        )
        return {
            "item_id": item.id,
            "requested_name": _clean_display_text(requested_name),
            "item_product_name": _clean_display_text(item.product_name),
            "matched_product_name": _clean_display_text(item.product_name),
            "characteristics_threshold": float(char_threshold),
            "score": round(float(score or 0.0), 4),
            "matched_points": int(matched_points),
            "total_points": int(total_points),
            "points": points,
            "requested_characteristics": requested,
            "source": f"on_demand:{offered_source}",
        }

    details_dict = item.similarity_points if isinstance(item.similarity_points, dict) else {}
    points = details_dict.get("points") if isinstance(details_dict.get("points"), list) else []
    matched_points = details_dict.get("matched_points")
    total_points = details_dict.get("total_points")
    try:
        matched_points = int(matched_points) if matched_points is not None else 0
    except Exception:
        matched_points = 0
    try:
        total_points = int(total_points) if total_points is not None else 0
    except Exception:
        total_points = 0

    matched_name = (
        (item.corrected_product_name or "").strip()
        or (_extract_match_name_from_notes(item.notes) or "").strip()
        or (details_dict.get("matched_product_name") or "").strip()
    )
    req_norm = _norm(requested_name)
    matched_norm = _norm(matched_name)
    same_requested = False
    if req_norm and matched_norm:
        same_requested = req_norm == matched_norm
        if not same_requested:
            same_requested = difflib.SequenceMatcher(None, req_norm, matched_norm).ratio() >= 0.90

    # Display-only mode: no recomputation here.
    # If this row was previously computed for a different requested product,
    # return empty detail for this requested_name.
    if requested and matched_name and not same_requested:
        return {
            "item_id": item.id,
            "requested_name": _clean_display_text(requested_name),
            "item_product_name": _clean_display_text(item.product_name),
            "matched_product_name": _clean_display_text(matched_name),
            "characteristics_threshold": float(item.similarity_threshold or char_threshold),
            "score": 0.0,
            "matched_points": 0,
            "total_points": len(requested),
            "points": [],
            "requested_characteristics": requested,
            "source": "similarity_points_mismatch",
        }

    return {
        "item_id": item.id,
        "requested_name": _clean_display_text(requested_name),
        "item_product_name": _clean_display_text(item.product_name),
        "matched_product_name": _clean_display_text(matched_name),
        "characteristics_threshold": float(
            details_dict.get("threshold")
            or item.similarity_threshold
            or char_threshold
        ),
        "score": round(float(item.similarity_score or 0.0), 4),
        "matched_points": matched_points,
        "total_points": total_points,
        "points": points,
        "requested_characteristics": requested,
        "source": details_dict.get("source") or "similarity_points",
    }


@router.patch("/items/{item_id}", response_model=InvoiceExtractionItemOut)
@router.put("/items/{item_id}", response_model=InvoiceExtractionItemOut)
def update_item(
    item_id: int,
    payload: InvoiceExtractionItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InvoiceExtractionItemOut:
    item = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.id == item_id,
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(item, key, value)
    if item.status is None:
        item.status = ExtractionStatus.TO_VERIFY

    invalidate_offer_analysis_results(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=item.campaign_id,
        item_ids=[item.id],
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    _try_warm_results_cache(db, current_user, item.campaign_id)
    return item


@router.post("/items/{item_id}/rematch", response_model=InvoiceExtractionItemOut)
def rematch_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InvoiceExtractionItemOut:
    item = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.id == item_id,
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    product_rows = (
        db.query(Product)
        .join(CampaignItem, CampaignItem.product_id == Product.id)
        .filter(
            CampaignItem.campaign_id == item.campaign_id,
            CampaignItem.tenant_id == current_user.tenant_id,
            Product.tenant_id == current_user.tenant_id,
        )
        .all()
    )
    if not product_rows:
        raise HTTPException(status_code=400, detail="No campaign products to match against")

    item.status = ExtractionStatus.TO_VERIFY
    # Recompute from the latest extracted name edited by the user.
    item.corrected_product_name = None
    item.notes = _preserve_invoice_components_notes_token(item.notes)
    invalidate_offer_analysis_results(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=item.campaign_id,
        item_ids=[item.id],
    )

    thresholds = get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)
    match_products_hybrid(
        [item],
        product_rows,
        threshold=0.70,
        embed_threshold=thresholds.confirm_threshold,
        embed_min_threshold=thresholds.verify_threshold,
        characteristics_match_threshold=thresholds.characteristics_threshold,
        preserve_corrected_on_reject=True,
        db=db,
    )

    if (item.notes or "") == "embedding_pending":
        try:
            from app.embedding_worker import run_worker

            run_worker(batch_size=200, poll_interval=0.1, once=True)
        except Exception:
            pass
        if (item.notes or "") == "embedding_pending":
            item.status = ExtractionStatus.TO_VERIFY
            item.notes = None
            match_products_hybrid(
                [item],
                product_rows,
                threshold=0.70,
                embed_threshold=thresholds.confirm_threshold,
                embed_min_threshold=thresholds.verify_threshold,
                characteristics_match_threshold=thresholds.characteristics_threshold,
                preserve_corrected_on_reject=True,
                db=db,
            )

    if bool(getattr(config, "OPENAI_INVOICE_COMPONENTS_PRECOMPUTE_ON_EXTRACT", True)):
        try:
            _extract_invoice_attachment_like_components(item, db=db)
        except Exception as exc:
            logger.warning(
                "Invoice GPT components precompute on rematch failed: tenant=%s item=%s err=%s",
                current_user.tenant_id,
                item.id,
                exc,
            )

    rebuild_characteristics_pair_cache(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=item.campaign_id,
        threshold=float(thresholds.characteristics_threshold),
        item_ids=[item.id],
    )
    db.commit()
    db.refresh(item)
    _try_warm_results_cache(db, current_user, item.campaign_id)
    supplier_name = None
    supplier_email = None
    if item.invoice is not None:
        supplier_email = item.invoice.sender_email
        supplier_name = item.invoice.sender_name or item.invoice.sender_email
    payload = InvoiceExtractionItemOut.from_orm(item).dict()
    payload["product_name"] = _clean_display_text(payload.get("product_name"))
    payload["description"] = _clean_display_text(payload.get("description"))
    payload["corrected_product_name"] = _clean_display_text(payload.get("corrected_product_name"))
    payload["supplier_name"] = _clean_display_text(supplier_name)
    payload["supplier_email"] = supplier_email
    return payload


@router.post("/items/{item_id}/recompute", response_model=InvoiceExtractionItemOut)
def recompute_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InvoiceExtractionItemOut:
    item = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.id == item_id,
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    invalidate_offer_analysis_results(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=item.campaign_id,
        item_ids=[item.id],
    )
    if bool(getattr(config, "OPENAI_INVOICE_COMPONENTS_PRECOMPUTE_ON_EXTRACT", True)):
        try:
            _extract_invoice_attachment_like_components(item, db=db)
        except Exception as exc:
            logger.warning(
                "Invoice item recompute components failed: tenant=%s item=%s err=%s",
                current_user.tenant_id,
                item.id,
                exc,
            )

    thresholds = get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)
    rebuild_characteristics_pair_cache(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=item.campaign_id,
        threshold=float(thresholds.characteristics_threshold),
        item_ids=[item.id],
    )
    db.commit()
    db.refresh(item)

    sender_email = item.invoice.sender_email if item.invoice is not None else None
    _auto_link_tech_sheets(
        db=db,
        current_user=current_user,
        campaign_id=item.campaign_id,
        invoice_id=item.invoice_id,
        sender_email=sender_email,
    )
    _try_warm_results_cache(db, current_user, item.campaign_id)

    supplier_name = None
    supplier_email = None
    if item.invoice is not None:
        supplier_email = item.invoice.sender_email
        supplier_name = item.invoice.sender_name or item.invoice.sender_email
    payload = InvoiceExtractionItemOut.from_orm(item).dict()
    payload["product_name"] = _clean_display_text(payload.get("product_name"))
    payload["description"] = _clean_display_text(payload.get("description"))
    payload["corrected_product_name"] = _clean_display_text(payload.get("corrected_product_name"))
    payload["supplier_name"] = _clean_display_text(supplier_name)
    payload["supplier_email"] = supplier_email
    return payload


@router.post("/items", response_model=InvoiceExtractionItemOut)
def create_item(
    payload: InvoiceExtractionItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InvoiceExtractionItemOut:
    invoice = (
        db.query(InboundInvoice)
        .filter(
            InboundInvoice.id == payload.invoice_id,
            InboundInvoice.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    item = InvoiceExtractionItem(
        tenant_id=current_user.tenant_id,
        invoice_id=invoice.id,
        campaign_id=invoice.campaign_id,
        supplier_id=None,
        source_engine="manual",
        status=payload.status or ExtractionStatus.CONFIRMED,
        product_name=payload.product_name.strip(),
        description=payload.description,
        unit=payload.unit,
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        currency=payload.currency or "DZD",
        confidence=None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    invalidate_offer_analysis_results(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=invoice.campaign_id,
        item_ids=[item.id],
    )
    db.commit()
    _try_warm_results_cache(db, current_user, invoice.campaign_id)

    supplier_name = invoice.sender_name or invoice.sender_email
    supplier_email = invoice.sender_email
    data = InvoiceExtractionItemOut.from_orm(item).dict()
    data["product_name"] = _clean_display_text(data.get("product_name"))
    data["description"] = _clean_display_text(data.get("description"))
    data["corrected_product_name"] = _clean_display_text(data.get("corrected_product_name"))
    data["supplier_name"] = _clean_display_text(supplier_name)
    data["supplier_email"] = supplier_email
    return data


@router.post("/recompute-invoice", response_model=dict)
def recompute_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    items = extract_invoice(
        invoice_id=invoice_id,
        force=True,
        db=db,
        current_user=current_user,
    )
    return {"updated": len(items), "invoice_id": invoice_id}


@router.post("/{invoice_id}/extract", response_model=list[InvoiceExtractionItemOut])
def extract_invoice(
    invoice_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[InvoiceExtractionItemOut]:
    invoice = (
        db.query(InboundInvoice)
        .filter(
            InboundInvoice.id == invoice_id,
            InboundInvoice.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    existing_items = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.invoice_id == invoice_id,
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
        )
        .order_by(InvoiceExtractionItem.created_at.desc())
        .all()
    )
    if existing_items and not force:
        logger.info(
            "Invoice extract skipped (already extracted): tenant=%s invoice=%s existing_items=%s",
            current_user.tenant_id,
            invoice_id,
            len(existing_items),
        )
        thresholds = get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)
        rebuild_characteristics_pair_cache(
            db=db,
            tenant_id=current_user.tenant_id,
            campaign_id=invoice.campaign_id,
            threshold=float(thresholds.characteristics_threshold),
            item_ids=[it.id for it in existing_items if it.id is not None],
        )
        db.commit()
        _auto_link_tech_sheets(
            db=db,
            current_user=current_user,
            campaign_id=invoice.campaign_id,
            invoice_id=invoice.id,
            sender_email=invoice.sender_email,
        )
        _try_warm_results_cache(db, current_user, invoice.campaign_id)
        return existing_items
    if force and existing_items:
        invalidate_offer_analysis_results(
            db=db,
            tenant_id=current_user.tenant_id,
            campaign_id=invoice.campaign_id,
            invoice_id=invoice_id,
        )
        # Keep manual corrections, replace only extracted lines.
        (
            db.query(InvoiceExtractionItem)
            .filter(
                InvoiceExtractionItem.invoice_id == invoice_id,
                InvoiceExtractionItem.tenant_id == current_user.tenant_id,
                InvoiceExtractionItem.source_engine != "manual",
            )
            .delete(synchronize_session=False)
        )
        db.flush()
    logger.info(
        "Invoice extract running: tenant=%s invoice=%s force=%s",
        current_user.tenant_id,
        invoice_id,
        force,
    )

    items = extract_line_items(invoice, db)
    if not items:
        # Persist invoice parse metadata and supplier format signature updates even
        # when no line items were extracted.
        db.commit()
        return []

    product_rows = (
        db.query(Product)
        .join(CampaignItem, CampaignItem.product_id == Product.id)
        .filter(
            CampaignItem.campaign_id == invoice.campaign_id,
            CampaignItem.tenant_id == invoice.tenant_id,
            Product.tenant_id == invoice.tenant_id,
        )
        .all()
    )
    thresholds = get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)
    match_products_hybrid(
        items,
        product_rows,
        threshold=0.70,
        embed_threshold=thresholds.confirm_threshold,
        embed_min_threshold=thresholds.verify_threshold,
        characteristics_match_threshold=thresholds.characteristics_threshold,
        db=db,
    )

    fallback_report = apply_invoice_fallback_for_low_gpt_score(invoice, items)
    if int(fallback_report.get("applied") or 0) > 0:
        logger.info(
            (
                "Invoice fallback applied: tenant=%s invoice=%s applied=%s triggered=%s "
                "model=%s min_gpt=%.2f"
            ),
            current_user.tenant_id,
            invoice_id,
            int(fallback_report.get("applied") or 0),
            int(fallback_report.get("triggered") or 0),
            str(fallback_report.get("model") or ""),
            float(fallback_report.get("min_gpt_score") or 0.0),
        )
        match_products_hybrid(
            items,
            product_rows,
            threshold=0.70,
            embed_threshold=thresholds.confirm_threshold,
            embed_min_threshold=thresholds.verify_threshold,
            characteristics_match_threshold=thresholds.characteristics_threshold,
            db=db,
        )

    for item in items:
        db.add(item)
    db.commit()

    if bool(getattr(config, "OPENAI_INVOICE_COMPONENTS_PRECOMPUTE_ON_EXTRACT", True)):
        persisted_items = (
            db.query(InvoiceExtractionItem)
            .filter(
                InvoiceExtractionItem.invoice_id == invoice_id,
                InvoiceExtractionItem.tenant_id == current_user.tenant_id,
            )
            .order_by(InvoiceExtractionItem.id.asc())
            .all()
        )
        for item in persisted_items:
            try:
                _extract_invoice_attachment_like_components(item, db=db)
            except Exception as exc:
                logger.warning(
                    "Invoice GPT components precompute failed: tenant=%s invoice=%s item_name=%s err=%s",
                    current_user.tenant_id,
                    invoice_id,
                    item.product_name,
                    exc,
                )
        db.commit()
    item_ids = [it.id for it in items if it.id is not None]
    if item_ids:
        rebuild_characteristics_pair_cache(
            db=db,
            tenant_id=current_user.tenant_id,
            campaign_id=invoice.campaign_id,
            threshold=float(thresholds.characteristics_threshold),
            item_ids=item_ids,
        )
        db.commit()
    _auto_link_tech_sheets(
        db=db,
        current_user=current_user,
        campaign_id=invoice.campaign_id,
        invoice_id=invoice.id,
        sender_email=invoice.sender_email,
    )
    _try_warm_results_cache(db, current_user, invoice.campaign_id)
    return items


@router.post("/items/confirm-sure", response_model=dict)
def confirm_sure_items(
    campaign_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    query = db.query(InvoiceExtractionItem).filter(
        InvoiceExtractionItem.tenant_id == current_user.tenant_id
    )
    if campaign_id is not None:
        query = query.filter(InvoiceExtractionItem.campaign_id == campaign_id)
    items = query.all()

    updated = 0
    for item in items:
        if item.status == ExtractionStatus.CONFIRMED:
            continue
        notes = item.notes or ""
        if "exact_match=" in notes:
            item.status = ExtractionStatus.CONFIRMED
            updated += 1
            continue
        score = _parse_score(notes)
        if score is not None and score >= config.OPENAI_MATCH_CONFIRM_THRESHOLD:
            item.status = ExtractionStatus.CONFIRMED
            updated += 1

    db.commit()
    _try_warm_results_cache(db, current_user, campaign_id)
    return {"updated": updated}


@router.post("/recompute", response_model=dict)
def recompute_items(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    items = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
            InvoiceExtractionItem.campaign_id == campaign_id,
        )
        .all()
    )
    if not items:
        return {"updated": 0}

    # Skip manual lines
    recompute_items = [it for it in items if (it.source_engine or "") != "manual"]
    if not recompute_items:
        return {"updated": 0}

    for it in recompute_items:
        it.status = ExtractionStatus.TO_VERIFY
        it.corrected_product_name = None
        it.corrected_unit = None
        it.corrected_quantity = None
        it.corrected_unit_price = None
        it.corrected_currency = None
        it.notes = _preserve_invoice_components_notes_token(it.notes)
    invalidate_offer_analysis_results(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=campaign_id,
        item_ids=[it.id for it in recompute_items if it.id is not None],
    )

    product_rows = (
        db.query(Product)
        .join(CampaignItem, CampaignItem.product_id == Product.id)
        .filter(
            CampaignItem.campaign_id == campaign_id,
            CampaignItem.tenant_id == current_user.tenant_id,
            Product.tenant_id == current_user.tenant_id,
        )
        .all()
    )
    thresholds = get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)
    match_products_hybrid(
        recompute_items,
        product_rows,
        threshold=0.70,
        embed_threshold=thresholds.confirm_threshold,
        embed_min_threshold=thresholds.verify_threshold,
        characteristics_match_threshold=thresholds.characteristics_threshold,
        db=db,
    )

    # If embeddings are pending, process one queue cycle immediately and retry matching.
    has_pending_embeddings = any((it.notes or "") == "embedding_pending" for it in recompute_items)
    if has_pending_embeddings:
        try:
            from app.embedding_worker import run_worker

            run_worker(batch_size=200, poll_interval=0.1, once=True)
        except Exception:
            # Keep pending status if queue processing fails; user can retry.
            pass

        # Re-run hybrid matching with user thresholds after queue pass.
        for it in recompute_items:
            if (it.notes or "") == "embedding_pending":
                it.status = ExtractionStatus.TO_VERIFY
                it.corrected_product_name = None
                it.notes = None

        match_products_hybrid(
            recompute_items,
            product_rows,
            threshold=0.70,
            embed_threshold=thresholds.confirm_threshold,
            embed_min_threshold=thresholds.verify_threshold,
            characteristics_match_threshold=thresholds.characteristics_threshold,
            db=db,
        )
    rebuild_characteristics_pair_cache(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=campaign_id,
        threshold=float(thresholds.characteristics_threshold),
        item_ids=[it.id for it in recompute_items if it.id is not None],
    )
    db.commit()
    _auto_link_tech_sheets(
        db=db,
        current_user=current_user,
        campaign_id=campaign_id,
    )
    _try_warm_results_cache(db, current_user, campaign_id)
    return {"updated": len(recompute_items)}


@router.post("/recompute-characteristics", response_model=dict)
def recompute_characteristics_only(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    items = (
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
            InvoiceExtractionItem.campaign_id == campaign_id,
        )
        .all()
    )
    item_ids = [it.id for it in items if it.id is not None]
    if not item_ids:
        return {"updated": 0, "pairs_upserted": 0}

    thresholds = get_user_embedding_thresholds(
        db, current_user.tenant_id, current_user.id
    )
    upserted = rebuild_characteristics_pair_cache(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=campaign_id,
        threshold=float(thresholds.characteristics_threshold),
        item_ids=item_ids,
    )
    db.commit()
    _try_warm_results_cache(db, current_user, campaign_id)
    return {
        "updated": len(item_ids),
        "pairs_upserted": int(upserted),
        "threshold": float(thresholds.characteristics_threshold),
    }

