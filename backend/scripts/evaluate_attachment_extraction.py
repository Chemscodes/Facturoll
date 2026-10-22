from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.campaign_attachment_extraction import extract_attachment_products_preview


def _iter_pdf_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".pdf" else []
    return sorted(p for p in path.rglob("*.pdf") if p.is_file())


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * q))
    idx = max(0, min(idx, len(ordered) - 1))
    return float(ordered[idx])


def _evaluate_one(pdf_path: Path, embedding_threshold: float | None) -> dict[str, Any]:
    started = time.perf_counter()
    payload = extract_attachment_products_preview(str(pdf_path), embedding_threshold=embedding_threshold)
    elapsed = time.perf_counter() - started

    products = payload.get("products") or []
    verify_count = sum(len(p.get("a_verifier") or []) for p in products)
    review_badge_count = sum(1 for p in products if p.get("needs_review"))
    low_quality_count = sum(1 for p in products if str(p.get("quality_level") or "") == "low")
    medium_quality_count = sum(1 for p in products if str(p.get("quality_level") or "") == "medium")
    high_quality_count = sum(1 for p in products if str(p.get("quality_level") or "") == "high")

    return {
        "file": str(pdf_path),
        "seconds": round(elapsed, 3),
        "engine": payload.get("engine"),
        "doc_type": payload.get("doc_type"),
        "items_count": int(payload.get("items_count") or 0),
        "products_count": len(products),
        "unassigned_count": len(payload.get("unassigned") or []),
        "verify_count": verify_count,
        "review_badge_count": review_badge_count,
        "quality_high_count": high_quality_count,
        "quality_medium_count": medium_quality_count,
        "quality_low_count": low_quality_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch evaluate campaign attachment extraction quality.")
    parser.add_argument("--input", required=True, help="PDF file or folder path.")
    parser.add_argument(
        "--embedding-threshold",
        type=float,
        default=None,
        help="Optional embedding threshold override for extraction preview.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max number of PDFs to process (0 = all).")
    parser.add_argument(
        "--output-json",
        default=str(ROOT_DIR / "reports" / "attachment_extraction_eval_summary.json"),
        help="Output JSON summary path.",
    )
    parser.add_argument(
        "--output-csv",
        default=str(ROOT_DIR / "reports" / "attachment_extraction_eval_rows.csv"),
        help="Output CSV rows path.",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    pdf_files = _iter_pdf_files(input_path)
    if args.limit and args.limit > 0:
        pdf_files = pdf_files[: args.limit]
    if not pdf_files:
        print(f"No PDF found at: {input_path}")
        return 1

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for idx, pdf in enumerate(pdf_files, start=1):
        try:
            row = _evaluate_one(pdf, args.embedding_threshold)
            rows.append(row)
            print(
                f"[{idx}/{len(pdf_files)}] OK {pdf.name} | "
                f"{row['seconds']}s | products={row['products_count']} | verify={row['verify_count']}"
            )
        except Exception as exc:  # pragma: no cover - best effort batch run
            errors.append({"file": str(pdf), "error": str(exc)})
            print(f"[{idx}/{len(pdf_files)}] ERROR {pdf.name}: {exc}")

    seconds = [float(r.get("seconds") or 0.0) for r in rows]
    products_total = sum(int(r.get("products_count") or 0) for r in rows)
    summary = {
        "input": str(input_path),
        "files_total": len(pdf_files),
        "files_success": len(rows),
        "files_error": len(errors),
        "seconds_avg": round(statistics.mean(seconds), 3) if seconds else 0.0,
        "seconds_p95": round(_percentile(seconds, 0.95), 3) if seconds else 0.0,
        "products_total": products_total,
        "products_avg_per_file": round(products_total / len(rows), 3) if rows else 0.0,
        "verify_total": sum(int(r.get("verify_count") or 0) for r in rows),
        "review_badge_total": sum(int(r.get("review_badge_count") or 0) for r in rows),
        "quality_high_total": sum(int(r.get("quality_high_count") or 0) for r in rows),
        "quality_medium_total": sum(int(r.get("quality_medium_count") or 0) for r in rows),
        "quality_low_total": sum(int(r.get("quality_low_count") or 0) for r in rows),
        "errors": errors,
        "rows": rows,
    }

    output_json = Path(args.output_json).expanduser()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    output_csv = Path(args.output_csv).expanduser()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    csv_headers = [
        "file",
        "seconds",
        "engine",
        "doc_type",
        "items_count",
        "products_count",
        "unassigned_count",
        "verify_count",
        "review_badge_count",
        "quality_high_count",
        "quality_medium_count",
        "quality_low_count",
    ]
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in csv_headers})

    print(f"\nSaved JSON summary: {output_json}")
    print(f"Saved CSV rows: {output_csv}")
    print(
        "Summary: "
        f"success={summary['files_success']}/{summary['files_total']} | "
        f"avg_s={summary['seconds_avg']} | p95_s={summary['seconds_p95']} | "
        f"products={summary['products_total']} | verify={summary['verify_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
