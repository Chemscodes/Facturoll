from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import config
from app.db import SessionLocal
from app.models import InboundInvoice
from app.services.invoice_extraction import extract_line_items


def _is_unknown_name(value: str | None) -> bool:
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return True
    if text == "unknown":
        return True
    return text.startswith("unknown ")


def _safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _item_digest_line(item) -> str:
    name = " ".join(str(item.product_name or "").strip().lower().split())
    qty = _safe_float(getattr(item, "quantity", None))
    unit_price = _safe_float(getattr(item, "unit_price", None))
    unit = " ".join(str(getattr(item, "unit", "") or "").strip().lower().split())
    source = " ".join(str(getattr(item, "source_engine", "") or "").strip().lower().split())
    return f"{name}|{unit}|{qty}|{unit_price}|{source}"


def _metrics_from_items(items: list) -> dict[str, object]:
    count = len(items)
    unknown_count = sum(1 for it in items if _is_unknown_name(getattr(it, "product_name", None)))
    missing_qty_count = sum(1 for it in items if _safe_float(getattr(it, "quantity", None)) is None)
    missing_price_count = sum(1 for it in items if _safe_float(getattr(it, "unit_price", None)) is None)
    confidences = [
        float(v)
        for v in (_safe_float(getattr(it, "confidence", None)) for it in items)
        if v is not None
    ]
    avg_conf = round(sum(confidences) / len(confidences), 6) if confidences else None
    digest_source = "\n".join(_item_digest_line(it) for it in items)
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
    return {
        "items_count": count,
        "unknown_count": unknown_count,
        "missing_qty_count": missing_qty_count,
        "missing_price_count": missing_price_count,
        "avg_confidence": avg_conf,
        "digest": digest,
    }


def _run_once(invoice: InboundInvoice, v2_enabled: bool) -> dict[str, object]:
    prev_flag = bool(getattr(config, "DOC_AI_FALLBACK_V2_ENABLED", False))
    config.DOC_AI_FALLBACK_V2_ENABLED = v2_enabled
    started = time.perf_counter()
    try:
        items = extract_line_items(invoice, db=None)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        out = _metrics_from_items(items)
        out["error"] = None
        out["elapsed_ms"] = elapsed_ms
        return out
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "items_count": 0,
            "unknown_count": 0,
            "missing_qty_count": 0,
            "missing_price_count": 0,
            "avg_confidence": None,
            "digest": None,
            "error": str(exc),
            "elapsed_ms": elapsed_ms,
        }
    finally:
        config.DOC_AI_FALLBACK_V2_ENABLED = prev_flag


def _pick_invoices(db, limit: int, campaign_id: int | None) -> list[InboundInvoice]:
    query = db.query(InboundInvoice)
    if campaign_id is not None:
        query = query.filter(InboundInvoice.campaign_id == campaign_id)
    rows = query.order_by(InboundInvoice.id.desc()).limit(max(limit * 20, 200)).all()
    selected: list[InboundInvoice] = []
    for row in rows:
        file_path = Path(str(row.file_path or ""))
        if file_path.suffix.lower() != ".pdf":
            continue
        if not file_path.exists():
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _compare_row(invoice: InboundInvoice) -> dict[str, object]:
    before = _run_once(invoice, v2_enabled=False)
    after = _run_once(invoice, v2_enabled=True)

    changed = False
    if before.get("error") or after.get("error"):
        changed = str(before.get("error")) != str(after.get("error"))
    else:
        changed = before.get("digest") != after.get("digest")

    return {
        "invoice_id": int(invoice.id or 0),
        "campaign_id": int(invoice.campaign_id or 0),
        "file_name": str(invoice.file_name or ""),
        "file_path": str(invoice.file_path or ""),
        "before_items": before.get("items_count"),
        "after_items": after.get("items_count"),
        "before_unknown": before.get("unknown_count"),
        "after_unknown": after.get("unknown_count"),
        "before_missing_qty": before.get("missing_qty_count"),
        "after_missing_qty": after.get("missing_qty_count"),
        "before_missing_price": before.get("missing_price_count"),
        "after_missing_price": after.get("missing_price_count"),
        "before_avg_confidence": before.get("avg_confidence"),
        "after_avg_confidence": after.get("avg_confidence"),
        "before_elapsed_ms": before.get("elapsed_ms"),
        "after_elapsed_ms": after.get("elapsed_ms"),
        "before_error": before.get("error"),
        "after_error": after.get("error"),
        "changed": changed,
        "before_digest": before.get("digest"),
        "after_digest": after.get("digest"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Document-AI fallback extraction with DOC_AI_FALLBACK_V2 flag OFF vs ON."
    )
    parser.add_argument("--limit", type=int, default=10, help="Number of PDF invoices to compare.")
    parser.add_argument("--campaign-id", type=int, default=None, help="Optional campaign filter.")
    parser.add_argument(
        "--output-json",
        default=str(Path("reports") / "doc_ai_fallback_compare_latest.json"),
    )
    parser.add_argument(
        "--output-csv",
        default=str(Path("reports") / "doc_ai_fallback_compare_latest.csv"),
    )
    args = parser.parse_args()

    if args.limit <= 0:
        raise SystemExit("--limit must be > 0")

    try:
        with SessionLocal() as db:
            invoices = _pick_invoices(db, limit=args.limit, campaign_id=args.campaign_id)
    except Exception as exc:
        print(f"Database connection failed: {exc}")
        return 2

    rows = [_compare_row(inv) for inv in invoices]
    changed_count = sum(1 for row in rows if bool(row.get("changed")))
    errors_before = sum(1 for row in rows if row.get("before_error"))
    errors_after = sum(1 for row in rows if row.get("after_error"))
    summary = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "limit_requested": args.limit,
        "campaign_id": args.campaign_id,
        "invoices_compared": len(rows),
        "changed_count": changed_count,
        "identical_count": max(0, len(rows) - changed_count),
        "errors_before": errors_before,
        "errors_after": errors_after,
        "v2_flag_current_runtime": bool(getattr(config, "DOC_AI_FALLBACK_V2_ENABLED", False)),
    }

    payload = {"summary": summary, "rows": rows}

    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    headers = sorted({k for row in rows for k in row.keys()}) if rows else []
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        if headers:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    print(f"Saved JSON: {output_json}")
    print(f"Saved CSV: {output_csv}")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
