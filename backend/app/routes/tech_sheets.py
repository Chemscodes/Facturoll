from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import config
from app.auth import get_current_user
from app.db import get_db
from app.models import (
    Campaign,
    TechSheet,
    User,
    InboundInvoice,
    InvoiceExtractionItem,
    TechSheetLink,
    ExtractionStatus,
)
from app.schemas import (
    TechSheetComparePayload,
    TechSheetExtractPayload,
    TechSheetOut,
    TechSheetLinkPayload,
    TechSheetLinkOut,
)
from app.services.tech_sheet_extraction import (
    compare_with_kv,
    compare_with_raw_text,
    extract_tech_sheet,
    _embed_texts,
    _canonical_key,
)
from app.services.openai_payload_cache import (
    get_openai_payload_cache,
    set_openai_payload_cache,
)
import difflib
from app.services.openai_embeddings import cosine_similarity
from app.services.invoice_extraction import (
    _normalize_name,
    compute_resemblance_points_for_items,
    rebuild_characteristics_pair_cache,
)
from app.settings_service import get_user_embedding_thresholds

router = APIRouter(prefix="/tech-sheets", tags=["tech-sheets"])
logger = logging.getLogger("app")


_INVOICE_LIKE_FILENAME_RE = re.compile(
    r"\b("
    r"facture|invoice|proforma|pro[\s_-]*forma|devis|quotation|"
    r"bon[\s_-]*de[\s_-]*commande|purchase[\s_-]*order|fp[\s._-]*n"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_invoice_text(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    invoice_terms = [
        "facture",
        "proforma",
        "invoice",
        "designation",
        "quantite",
        "qte",
        "pu",
        "prix unitaire",
        "montant",
        "total",
        "tva",
        "ttc",
        "ht",
    ]
    hits = sum(1 for term in invoice_terms if term in low)
    has_table_header = "designation" in low and ("qte" in low or "quantite" in low)
    return has_table_header or hits >= 4


def _is_invoice_like_tech_doc(
    *,
    file_name: str | None,
    parse_engine: str | None,
    raw_text: str | None = None,
    characteristics: object | None,
) -> bool:
    engine = (parse_engine or "").strip().lower()
    if engine.endswith("_invoice") or engine == "invoice":
        return True

    if _looks_like_invoice_text(str(raw_text or "")):
        return True

    if isinstance(characteristics, dict):
        doc_type = str(characteristics.get("doc_type_guess") or "").strip().lower()
        if doc_type == "invoice":
            return True
        # When extraction finds strong table-like invoice keys, avoid storing as tech sheet.
        kv = characteristics.get("kv")
        if isinstance(kv, dict):
            invoice_like_keys = {"designation", "qte", "quantite", "pu", "montant", "total", "tva", "ttc", "ht"}
            low_keys = {str(k).strip().lower() for k in kv.keys()}
            if len(low_keys.intersection(invoice_like_keys)) >= 3:
                return True

    if _INVOICE_LIKE_FILENAME_RE.search((file_name or "").strip().lower()):
        return True

    return False


def _compute_file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _build_tech_sheet_hash_index(
    db: Session,
    tenant_id: int,
    campaign_id: int,
) -> dict[str, TechSheet]:
    rows = (
        db.query(TechSheet)
        .filter(
            TechSheet.tenant_id == tenant_id,
            TechSheet.campaign_id == campaign_id,
        )
        .order_by(TechSheet.created_at.desc(), TechSheet.id.desc())
        .all()
    )
    index: dict[str, TechSheet] = {}
    dirty = False
    for row in rows:
        file_hash = (row.file_hash or "").strip().lower()
        if not file_hash:
            stored_path = Path(str(row.file_path or ""))
            if stored_path.exists():
                computed = _compute_file_sha256(stored_path)
                if computed:
                    file_hash = computed
                    row.file_hash = file_hash
                    dirty = True
        if file_hash and file_hash not in index:
            index[file_hash] = row
    if dirty:
        db.flush()
    return index


@router.get("", response_model=list[TechSheetOut])
def list_tech_sheets(
    campaign_id: int | None = None,
    sender_email: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TechSheetOut]:
    query = db.query(TechSheet).filter(TechSheet.tenant_id == current_user.tenant_id)
    if campaign_id is not None:
        query = query.filter(TechSheet.campaign_id == campaign_id)
    if sender_email is not None:
        query = query.filter(TechSheet.sender_email == sender_email)
    return query.order_by(TechSheet.created_at.desc()).all()


@router.post("/extract", response_model=list[TechSheetOut])
def extract_from_files(
    payload: TechSheetExtractPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TechSheetOut]:
    if not payload.file_paths:
        raise HTTPException(status_code=400, detail="file_paths is required")

    hash_index = _build_tech_sheet_hash_index(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=payload.campaign_id,
    )
    results: list[TechSheet] = []
    for file_path in payload.file_paths:
        path = Path(file_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

        file_hash = _compute_file_sha256(path)
        if not file_hash:
            raise HTTPException(status_code=400, detail=f"Unable to read file for hashing: {file_path}")

        # Cost guard: deduplicate by PDF content hash in campaign scope.
        existing = hash_index.get(file_hash)
        if existing:
            if bool(getattr(config, "TECH_SHEET_SKIP_INVOICE_LIKE", True)) and _is_invoice_like_tech_doc(
                file_name=existing.file_name,
                parse_engine=existing.parse_engine,
                raw_text=existing.raw_text,
                characteristics=existing.characteristics,
            ):
                logger.info(
                    "Tech sheet extract skip existing invoice-like doc: tenant=%s campaign=%s sheet=%s file=%s",
                    current_user.tenant_id,
                    payload.campaign_id,
                    existing.id,
                    existing.file_name,
                )
                continue
            results.append(existing)
            continue

        raw_text, characteristics, score, engine = extract_tech_sheet(str(path))
        if bool(getattr(config, "TECH_SHEET_SKIP_INVOICE_LIKE", True)) and _is_invoice_like_tech_doc(
            file_name=path.name,
            parse_engine=engine,
            raw_text=raw_text,
            characteristics=characteristics,
        ):
            logger.info(
                "Tech sheet extract skipped invoice-like file: tenant=%s campaign=%s file=%s engine=%s",
                current_user.tenant_id,
                payload.campaign_id,
                path.name,
                engine,
            )
            continue
        sheet = TechSheet(
            tenant_id=current_user.tenant_id,
            campaign_id=payload.campaign_id,
            sender_email=payload.sender_email,
            file_name=path.name,
            file_path=str(path),
            file_hash=file_hash,
            parse_engine=engine,
            parse_score=score,
            raw_text=raw_text,
            characteristics=characteristics,
        )
        db.add(sheet)
        results.append(sheet)
        hash_index[file_hash] = sheet

    db.commit()
    for sheet in results:
        db.refresh(sheet)
    try:
        link_tech_sheets(
            TechSheetLinkPayload(
                campaign_id=payload.campaign_id,
                sender_email=payload.sender_email,
                min_score=0.60,
                max_text_chars=2000,
            ),
            db=db,
            current_user=current_user,
        )
    except Exception as exc:
        logger.warning(
            "Auto tech-sheet link failed after extract: tenant=%s campaign=%s error=%s",
            current_user.tenant_id,
            payload.campaign_id,
            exc,
        )
    return results


@router.post("/extract-folder", response_model=list[TechSheetOut])
def extract_from_folder(
    payload: TechSheetExtractPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TechSheetOut]:
    if not payload.folder_path:
        raise HTTPException(status_code=400, detail="folder_path is required")
    folder = Path(payload.folder_path)
    if not folder.exists():
        raise HTTPException(status_code=404, detail=f"Folder not found: {payload.folder_path}")

    hash_index = _build_tech_sheet_hash_index(
        db=db,
        tenant_id=current_user.tenant_id,
        campaign_id=payload.campaign_id,
    )
    results: list[TechSheet] = []
    for path in folder.glob("*.pdf"):
        file_hash = _compute_file_sha256(path)
        if not file_hash:
            raise HTTPException(
                status_code=400,
                detail=f"Unable to read file for hashing: {str(path)}",
            )
        # Skip already stored (campaign scope, same content hash).
        if file_hash in hash_index:
            existing = hash_index.get(file_hash)
            if existing and bool(getattr(config, "TECH_SHEET_SKIP_INVOICE_LIKE", True)) and _is_invoice_like_tech_doc(
                file_name=existing.file_name,
                parse_engine=existing.parse_engine,
                raw_text=existing.raw_text,
                characteristics=existing.characteristics,
            ):
                logger.info(
                    "Tech sheet extract-folder skip existing invoice-like doc: tenant=%s campaign=%s sheet=%s file=%s",
                    current_user.tenant_id,
                    payload.campaign_id,
                    existing.id,
                    existing.file_name,
                )
            continue
        raw_text, characteristics, score, engine = extract_tech_sheet(str(path))
        if bool(getattr(config, "TECH_SHEET_SKIP_INVOICE_LIKE", True)) and _is_invoice_like_tech_doc(
            file_name=path.name,
            parse_engine=engine,
            raw_text=raw_text,
            characteristics=characteristics,
        ):
            logger.info(
                "Tech sheet extract-folder skipped invoice-like file: tenant=%s campaign=%s file=%s engine=%s",
                current_user.tenant_id,
                payload.campaign_id,
                path.name,
                engine,
            )
            continue
        sheet = TechSheet(
            tenant_id=current_user.tenant_id,
            campaign_id=payload.campaign_id,
            sender_email=payload.sender_email,
            file_name=path.name,
            file_path=str(path),
            file_hash=file_hash,
            parse_engine=engine,
            parse_score=score,
            raw_text=raw_text,
            characteristics=characteristics,
        )
        db.add(sheet)
        results.append(sheet)
        hash_index[file_hash] = sheet

    db.commit()
    for sheet in results:
        db.refresh(sheet)
    try:
        link_tech_sheets(
            TechSheetLinkPayload(
                campaign_id=payload.campaign_id,
                sender_email=payload.sender_email,
                min_score=0.60,
                max_text_chars=2000,
            ),
            db=db,
            current_user=current_user,
        )
    except Exception as exc:
        logger.warning(
            "Auto tech-sheet link failed after extract-folder: tenant=%s campaign=%s error=%s",
            current_user.tenant_id,
            payload.campaign_id,
            exc,
        )
    return results


@router.post("/compare", response_model=dict)
def compare_tech_sheets(
    payload: TechSheetComparePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    if not payload.requested:
        raise HTTPException(status_code=400, detail="requested is required")
    query = db.query(TechSheet).filter(TechSheet.tenant_id == current_user.tenant_id)
    query = query.filter(TechSheet.campaign_id == payload.campaign_id)
    if payload.sender_email:
        query = query.filter(TechSheet.sender_email == payload.sender_email)
    sheets = query.order_by(TechSheet.created_at.desc()).all()

    results = []
    for sheet in sheets:
        sheet_result = {
            "tech_sheet_id": sheet.id,
            "file_name": sheet.file_name,
            "file_path": sheet.file_path,
            "parse_score": sheet.parse_score,
            "results": [],
        }
        for product in payload.requested:
            req_list = [{"key": c.key, "value": c.value} for c in product.characteristics]
            kv_cmp = compare_with_kv(sheet.characteristics or {}, req_list)
            raw_cmp_all = compare_with_raw_text(sheet.raw_text or "", req_list)

            # Rule: if a key is already extracted by regex/kv, do not use embedding for that key
            # in effective scoring. Keep those embedding matches as candidates for later review.
            regex_keys_taken: set[str] = {
                str(m.get("key", "")).strip().lower()
                for m in kv_cmp.get("matches", [])
                if m.get("found") is not None
            }
            raw_used_matches = []
            raw_ignored_matches = []
            for m in raw_cmp_all.get("matches", []):
                key = _canonical_key(str(m.get("key", ""))).strip().lower()
                if key in regex_keys_taken:
                    raw_ignored_matches.append(m)
                else:
                    raw_used_matches.append(m)

            raw_used_score = 0.0
            if raw_used_matches:
                raw_used_score = sum(float(m.get("score") or 0.0) for m in raw_used_matches) / len(
                    raw_used_matches
                )
            raw_cmp = {
                "score": round(raw_used_score, 4),
                "matches": raw_used_matches,
                "ignored_by_regex": raw_ignored_matches,
            }
            entry = {
                "product_name": product.name,
                "raw_text": raw_cmp,
                "raw_text_candidates": raw_cmp_all,
                "kv": kv_cmp,
            }
            sheet_result["results"].append(entry)
        results.append(sheet_result)

    return {"count": len(results), "sheets": results}


def _sheet_name_text(sheet: TechSheet, max_chars: int) -> str:
    raw = sheet.raw_text or ""
    file_stem = (sheet.file_name or "").rsplit(".", 1)[0]
    file_stem = re.sub(r"[_\-]+", " ", file_stem).strip()
    file_stem = re.sub(r"\s+", " ", file_stem)

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return file_stem[:max_chars]

    product_hints = (
        "autoclave",
        "thermosoude",
        "soudeuse",
        "compresseur",
        "adoucisseur",
        "laveur",
        "desinfect",
        "sterilis",
        "panier",
        "pistolet",
        "classe",
        "litre",
        "model",
        "modele",
        "marque",
    )
    noise_hints = (
        "www.",
        "http",
        "@",
        "tel",
        "tél",
        "fax",
        "email",
        "adresse",
        "vicenza",
        "italy",
    )

    scored: list[tuple[float, int, str]] = []
    for idx, line in enumerate(lines[:160]):
        if len(line) < 3 or len(line) > 180:
            continue
        alpha = sum(ch.isalpha() for ch in line)
        if alpha / max(len(line), 1) < 0.25:
            continue

        low = line.lower()
        score = 0.0
        if any(h in low for h in product_hints):
            score += 4.0
        if re.search(r"\b[A-Z]{1,4}\s?-?\d{1,4}\b", line):
            score += 2.0
        if re.search(r"\b\d+(?:[.,]\d+)?\s?(?:l|ml|kw|w|v|hz|kg|mm|cm|m)\b", low):
            score += 1.5
        if any(h in low for h in noise_hints):
            score -= 3.0
        score += min(len(line), 120) / 120.0
        score += max(0.0, 1.2 - (idx * 0.03))
        scored.append((score, idx, line))

    best_lines: list[str] = []
    seen: set[str] = set()
    for _score, _idx, line in sorted(scored, key=lambda x: (x[0], -x[1]), reverse=True):
        key = re.sub(r"\s+", " ", line.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        best_lines.append(line)
        if len(best_lines) >= 3:
            break

    parts: list[str] = []
    if file_stem:
        parts.append(file_stem)
    parts.extend(best_lines)
    if not parts:
        return file_stem[:max_chars]
    text = " | ".join(parts)
    return text[:max_chars]


def _normalize_sender(email: str | None) -> str | None:
    value = (email or "").strip().lower()
    return value or None


def _item_group_key(item: InvoiceExtractionItem, inv: InboundInvoice) -> str | None:
    sender = _normalize_sender(inv.sender_email)
    if sender:
        return f"email:{sender}"
    return None


def _is_product_candidate(item: InvoiceExtractionItem) -> bool:
    # Keep only real product lines from invoice extraction.
    notes = (item.notes or "").strip().lower()
    if notes == "admin_line":
        return False
    name = (item.corrected_product_name or item.product_name or "").strip()
    desc = (item.description or "").strip()
    if not name and not desc:
        return False
    if name.upper() in {"UNKNOWN", "N/A", "-"} and not desc:
        return False
    text = f"{name} {desc}".strip()
    if not any(ch.isalpha() for ch in text):
        return False
    return True


def _sheet_group_key(sheet: TechSheet) -> str | None:
    sender = _normalize_sender(sheet.sender_email)
    if sender:
        return f"email:{sender}"
    return None


def _link_item_stable_key(item_row: dict) -> str:
    item = item_row["item"]
    inv = item_row["invoice"]
    payload = {
        "file_name": str(inv.file_name or "").strip(),
        "sender_email": str(inv.sender_email or "").strip().lower(),
        "sender_name": str(inv.sender_name or "").strip().lower(),
        "text": str(item_row.get("text") or "").strip(),
        "norm": str(item_row.get("norm") or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _link_group_signature(
    *,
    group_key: str,
    item_rows: list[dict],
    sheet_rows: list[dict],
    min_score: float,
    max_text_chars: int,
) -> str:
    payload = {
        "group": group_key,
        "embedding_model": str(getattr(config, "OPENAI_EMBEDDING_MODEL", "") or ""),
        "min_score": round(float(min_score), 4),
        "max_text_chars": int(max_text_chars),
        "items": [
            {
                "stable_key": _link_item_stable_key(row),
                "norm": str(row.get("norm") or "").strip(),
            }
            for row in sorted(item_rows, key=lambda r: (_link_item_stable_key(r), str(r.get("norm") or "")))
        ],
        "sheets": [
            {
                "sheet_id": int(row["sheet"].id or 0),
                "file_name": str(row["sheet"].file_name or "").strip(),
                "norm": str(row.get("norm") or "").strip(),
            }
            for row in sorted(sheet_rows, key=lambda r: (int(r["sheet"].id or 0), str(r.get("norm") or "")))
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sort_link_item_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            _link_item_stable_key(row),
            str(row["invoice"].file_name or "").strip().lower(),
            str(row["item"].product_name or "").strip().lower(),
            str(row["item"].description or "").strip().lower(),
        ),
    )


def _sort_link_sheet_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            str(row["sheet"].file_name or "").strip().lower(),
            str(row.get("norm") or "").strip(),
            str(row["sheet"].file_path or "").strip().lower(),
        ),
    )


def _compute_group_link_assignments(
    *,
    group_item_rows: list[dict],
    group_sheet_rows: list[dict],
    emb_map: dict[str, list[float]],
    min_score: float,
) -> dict[int, tuple[int, float, str]]:
    candidates: list[tuple[float, int, int, str]] = []
    for item_idx, item_row in enumerate(group_item_rows):
        item_norm = item_row["norm"]
        item_emb = emb_map.get(item_norm)
        for sheet_idx, sheet_row in enumerate(group_sheet_rows):
            sheet_norm = sheet_row["norm"]
            score = 0.0
            method: Optional[str] = None

            if item_norm and sheet_norm and (
                item_norm == sheet_norm
                or item_norm in sheet_norm
                or sheet_norm in item_norm
            ):
                score = 1.0
                method = "exact"
            else:
                fuzzy = (
                    difflib.SequenceMatcher(None, item_norm, sheet_norm).ratio()
                    if item_norm and sheet_norm
                    else 0.0
                )
                if fuzzy >= 0.80:
                    score = fuzzy
                    method = "fuzzy"
                else:
                    sheet_emb = emb_map.get(sheet_norm)
                    if item_emb is not None and sheet_emb is not None:
                        score = cosine_similarity(item_emb, sheet_emb)
                        method = "embedding"

            if method and score >= min_score:
                candidates.append((score, item_idx, sheet_idx, method))

    candidates.sort(key=lambda x: x[0], reverse=True)
    assigned_items: set[int] = set()
    assigned_sheets: set[int] = set()
    assignments: dict[int, tuple[int, float, str]] = {}
    for score, item_idx, sheet_idx, method in candidates:
        if item_idx in assigned_items or sheet_idx in assigned_sheets:
            continue
        assigned_items.add(item_idx)
        assigned_sheets.add(sheet_idx)
        assignments[item_idx] = (sheet_idx, score, method)
    return assignments


@router.post("/link", response_model=list[TechSheetLinkOut])
def link_tech_sheets(
    payload: TechSheetLinkPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TechSheetLinkOut]:
    # Gather invoice items (only from this tenant/campaign)
    items = (
        db.query(InvoiceExtractionItem, InboundInvoice)
        .join(InboundInvoice, InboundInvoice.id == InvoiceExtractionItem.invoice_id)
        .filter(
            InvoiceExtractionItem.tenant_id == current_user.tenant_id,
            InvoiceExtractionItem.campaign_id == payload.campaign_id,
            InboundInvoice.tenant_id == current_user.tenant_id,
        )
        .all()
    )
    if payload.invoice_id is not None:
        items = [(item, inv) for item, inv in items if item.invoice_id == payload.invoice_id]
    if payload.invoice_item_id is not None:
        items = [(item, inv) for item, inv in items if item.id == payload.invoice_item_id]
    if payload.sender_email:
        items = [
            (item, inv)
            for item, inv in items
            if (inv.sender_email or "").lower() == payload.sender_email.lower()
        ]
    items = [(item, inv) for item, inv in items if _is_product_candidate(item)]

    sheets_query = db.query(TechSheet).filter(
        TechSheet.tenant_id == current_user.tenant_id,
        TechSheet.campaign_id == payload.campaign_id,
    )
    # Ignore rows that are detected as invoice-like docs; they pollute linking.
    sheets_query = sheets_query.filter(
        or_(TechSheet.parse_engine.is_(None), ~TechSheet.parse_engine.ilike("%_invoice"))
    )
    if payload.sender_email:
        sheets_query = sheets_query.filter(TechSheet.sender_email == payload.sender_email)
    sheets = sheets_query.all()
    if bool(getattr(config, "TECH_SHEET_LINK_STRICT_FILTER", False)):
        sheets = [
            sheet
            for sheet in sheets
            if not _is_invoice_like_tech_doc(
                file_name=sheet.file_name,
                parse_engine=sheet.parse_engine,
                raw_text=sheet.raw_text,
                characteristics=sheet.characteristics,
            )
        ]

    if not items or not sheets:
        return []

    # Build normalized texts and strict sender-scoped groups.
    item_rows: list[dict] = []
    all_norm_texts: list[str] = []
    for item, inv in items:
        group_key = _item_group_key(item, inv)
        if not group_key:
            continue
        # Link primarily from the extracted invoice product name; corrected name is
        # only a fallback when product_name is empty.
        base_name = (item.product_name or "").strip()
        if not base_name:
            base_name = (item.corrected_product_name or "").strip()
        text = f"{base_name} {item.description or ''}".strip()
        if not text:
            continue
        norm_text = _normalize_name(text)
        if not norm_text:
            continue
        item_rows.append(
            {
                "item": item,
                "invoice": inv,
                "text": text,
                "norm": norm_text,
                "group": group_key,
            }
        )
        all_norm_texts.append(norm_text)

    sheet_rows: list[dict] = []
    for sheet in sheets:
        group_key = _sheet_group_key(sheet)
        if not group_key:
            continue
        text = _sheet_name_text(sheet, payload.max_text_chars)
        if not text:
            continue
        norm_text = _normalize_name(text)
        if not norm_text:
            continue
        sheet_rows.append(
            {
                "sheet": sheet,
                "text": text,
                "norm": norm_text,
                "group": group_key,
            }
        )
        all_norm_texts.append(norm_text)

    if not item_rows or not sheet_rows:
        return []

    item_rows_by_group: dict[str, list[dict]] = {}
    for row in item_rows:
        item_rows_by_group.setdefault(str(row["group"]), []).append(row)
    sheet_rows_by_group: dict[str, list[dict]] = {}
    for row in sheet_rows:
        sheet_rows_by_group.setdefault(str(row["group"]), []).append(row)

    link_cache_model = str(getattr(config, "OPENAI_EMBEDDING_MODEL", "") or "embedding")
    assignments_by_item_id: dict[int, tuple[TechSheet, float, str]] = {}
    groups_to_compute: list[tuple[str, list[dict], list[dict], str]] = []
    all_norm_texts = []

    for group_key, raw_group_items in item_rows_by_group.items():
        raw_group_sheets = sheet_rows_by_group.get(group_key) or []
        if not raw_group_items or not raw_group_sheets:
            continue
        group_items = _sort_link_item_rows(raw_group_items)
        group_sheets = _sort_link_sheet_rows(raw_group_sheets)
        cache_key = _link_group_signature(
            group_key=group_key,
            item_rows=group_items,
            sheet_rows=group_sheets,
            min_score=payload.min_score,
            max_text_chars=payload.max_text_chars,
        )
        cached_payload = get_openai_payload_cache(
            cache_type="tech_sheet_link_group",
            model=link_cache_model,
            cache_key=cache_key,
        )
        if isinstance(cached_payload, dict) and isinstance(cached_payload.get("pairs"), list):
            restored_any = False
            for pair in cached_payload.get("pairs") or []:
                if not isinstance(pair, dict):
                    continue
                try:
                    item_idx = int(pair.get("item_pos"))
                    sheet_idx = int(pair.get("sheet_pos"))
                    score = float(pair.get("score") or 0.0)
                except Exception:
                    continue
                if item_idx < 0 or sheet_idx < 0:
                    continue
                if item_idx >= len(group_items) or sheet_idx >= len(group_sheets):
                    continue
                method = str(pair.get("method") or "embedding").strip() or "embedding"
                item = group_items[item_idx]["item"]
                sheet = group_sheets[sheet_idx]["sheet"]
                if item.id is None or sheet.id is None:
                    continue
                assignments_by_item_id[int(item.id)] = (sheet, score, method)
                restored_any = True
            if restored_any:
                continue

        groups_to_compute.append((group_key, group_items, group_sheets, cache_key))
        for row in group_items:
            all_norm_texts.append(row["norm"])
        for row in group_sheets:
            all_norm_texts.append(row["norm"])

    emb_map = _embed_texts(all_norm_texts) if all_norm_texts else {}

    for group_key, group_items, group_sheets, cache_key in groups_to_compute:
        group_emb_map = {
            text: vector
            for text, vector in emb_map.items()
            if text in {row["norm"] for row in group_items} or text in {row["norm"] for row in group_sheets}
        }
        group_assignments = _compute_group_link_assignments(
            group_item_rows=group_items,
            group_sheet_rows=group_sheets,
            emb_map=group_emb_map,
            min_score=payload.min_score,
        )
        cache_pairs: list[dict[str, object]] = []
        for item_idx, (sheet_idx, score, method) in group_assignments.items():
            item = group_items[item_idx]["item"]
            sheet = group_sheets[sheet_idx]["sheet"]
            if item.id is None or sheet.id is None:
                continue
            assignments_by_item_id[int(item.id)] = (sheet, score, method)
            cache_pairs.append(
                {
                    "item_pos": item_idx,
                    "sheet_pos": sheet_idx,
                    "score": round(float(score), 6),
                    "method": method,
                }
            )
        set_openai_payload_cache(
            cache_type="tech_sheet_link_group",
            model=link_cache_model,
            cache_key=cache_key,
            payload={"pairs": cache_pairs},
        )

    item_ids = [row["item"].id for row in item_rows]
    existing_links_by_item_id: dict[int, TechSheetLink] = {}
    if item_ids:
        existing_links = (
            db.query(TechSheetLink)
            .filter(
                TechSheetLink.tenant_id == current_user.tenant_id,
                TechSheetLink.campaign_id == payload.campaign_id,
                TechSheetLink.invoice_item_id.in_(item_ids),
            )
            .all()
        )
        existing_links_by_item_id = {
            int(link.invoice_item_id): link
            for link in existing_links
            if link.invoice_item_id is not None
        }

    links_changed = False
    for item_row in item_rows:
        item = item_row["item"]
        if item.id is None:
            continue
        existing = existing_links_by_item_id.get(int(item.id))
        desired = assignments_by_item_id.get(int(item.id))
        if desired is None:
            if existing is not None:
                links_changed = True
                break
            continue
        desired_sheet, desired_score, desired_method = desired
        desired_sheet_id = int(desired_sheet.id or 0)
        existing_sheet_id = int(existing.tech_sheet_id or 0) if existing else 0
        existing_score = float(existing.score or 0.0) if existing else 0.0
        existing_method = str(existing.method or "").strip() if existing else ""
        if (
            existing is None
            or existing_sheet_id != desired_sheet_id
            or abs(existing_score - float(desired_score)) > 1e-6
            or existing_method != str(desired_method or "").strip()
        ):
            links_changed = True
            break

    if links_changed and item_ids:
        (
            db.query(TechSheetLink)
            .filter(
                TechSheetLink.tenant_id == current_user.tenant_id,
                TechSheetLink.campaign_id == payload.campaign_id,
                TechSheetLink.invoice_item_id.in_(item_ids),
            )
            .delete(synchronize_session=False)
        )

    results: list[TechSheetLinkOut] = []
    for item_row in item_rows:
        item = item_row["item"]
        best_sheet = None
        best_score = 0.0
        best_method: Optional[str] = None
        assignment = assignments_by_item_id.get(int(item.id)) if item.id is not None else None
        if assignment is not None:
            best_sheet, best_score, best_method = assignment

            if links_changed:
                link = TechSheetLink(
                    tenant_id=current_user.tenant_id,
                    campaign_id=item.campaign_id,
                    supplier_id=item.supplier_id,
                    invoice_item_id=item.id,
                    tech_sheet_id=best_sheet.id,
                    score=best_score,
                    method=best_method or "embedding",
                )
                db.add(link)

        results.append(
            TechSheetLinkOut(
                invoice_item_id=item.id,
                product_name=(item.product_name or item.corrected_product_name or ""),
                tech_sheet_id=best_sheet.id if best_sheet else None,
                tech_sheet_file=best_sheet.file_name if best_sheet else None,
                score=round(best_score if best_score > 0 else 0.0, 4),
                method=best_method,
            )
        )

    if not links_changed:
        return results

    scoped_items = [row["item"] for row in item_rows]
    try:
        from app.routes.invoice_extractions import invalidate_offer_analysis_results

        invalidate_offer_analysis_results(
            db=db,
            tenant_id=current_user.tenant_id,
            campaign_id=payload.campaign_id,
            item_ids=[it.id for it in scoped_items if it.id is not None],
        )
    except Exception as exc:
        logger.warning(
            "Offer analysis invalidation skipped after relink: tenant=%s campaign=%s error=%s",
            current_user.tenant_id,
            payload.campaign_id,
            exc,
        )
    db.commit()
    try:
        thresholds = get_user_embedding_thresholds(db, current_user.tenant_id, current_user.id)
        compute_resemblance_points_for_items(
            db=db,
            items=scoped_items,
            threshold=float(thresholds.characteristics_threshold),
        )
        rebuild_characteristics_pair_cache(
            db=db,
            tenant_id=current_user.tenant_id,
            campaign_id=payload.campaign_id,
            threshold=float(thresholds.characteristics_threshold),
            item_ids=[it.id for it in scoped_items if it.id is not None],
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Post-link characteristic refresh skipped: tenant=%s campaign=%s error=%s",
            current_user.tenant_id,
            payload.campaign_id,
            exc,
        )
    try:
        from app.routes.campaigns import (
            _campaign_effective_processing_mode,
            warm_results_cache_for_campaign,
        )

        campaign = (
            db.query(Campaign)
            .filter(
                Campaign.id == payload.campaign_id,
                Campaign.tenant_id == current_user.tenant_id,
            )
            .first()
        )
        if (
            campaign is not None
            and _campaign_effective_processing_mode(db, campaign) == "strict"
        ):
            warm_results_cache_for_campaign(
                campaign_id=payload.campaign_id,
                db=db,
                current_user=current_user,
            )
    except Exception as exc:
        logger.warning(
            "Strict results warm after tech-sheet link failed: tenant=%s campaign=%s error=%s",
            current_user.tenant_id,
            payload.campaign_id,
            exc,
        )
    return results


@router.post("/relink", response_model=list[TechSheetLinkOut])
def relink_tech_sheets(
    payload: TechSheetLinkPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TechSheetLinkOut]:
    return link_tech_sheets(payload, db=db, current_user=current_user)
