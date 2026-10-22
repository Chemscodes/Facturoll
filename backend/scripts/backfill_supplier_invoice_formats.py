from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.models import InboundInvoice, SupplierInvoiceFormat
from app.services.invoice_extraction import _detect_signature, _extract_pdf_text, _store_supplier_format


def run(tenant_id: int | None, apply: bool) -> int:
    db = SessionLocal()
    try:
        query = db.query(InboundInvoice).order_by(InboundInvoice.id.desc())
        if tenant_id is not None:
            query = query.filter(InboundInvoice.tenant_id == tenant_id)
        invoices = query.all()

        existing_by_key: dict[tuple[int, str], str] = {
            (int(row.tenant_id), (row.sender_email or "").strip().lower()): str(row.signature or "")
            for row in db.query(SupplierInvoiceFormat).all()
            if row.sender_email
        }

        signatures_by_key: dict[tuple[int, str], Counter[str]] = {}
        latest_signature_by_key: dict[tuple[int, str], str] = {}

        scanned = 0
        with_sender = 0
        with_existing_file = 0
        detected_signature_rows = 0

        for invoice in invoices:
            scanned += 1
            sender = (invoice.sender_email or "").strip().lower()
            if not sender:
                continue
            with_sender += 1

            file_path = (invoice.file_path or "").strip()
            if not file_path or not Path(file_path).exists():
                continue
            with_existing_file += 1

            text = _extract_pdf_text(file_path) or ""
            signature = _detect_signature(text)
            if not signature:
                continue

            detected_signature_rows += 1
            key = (int(invoice.tenant_id), sender)
            signatures_by_key.setdefault(key, Counter())[signature] += 1
            latest_signature_by_key.setdefault(key, signature)

        selected_by_key: dict[tuple[int, str], str] = {}
        for key, counter in signatures_by_key.items():
            if not counter:
                continue
            max_count = max(counter.values())
            candidates = [sig for sig, count in counter.items() if count == max_count]
            latest_sig = latest_signature_by_key.get(key)
            if latest_sig in candidates:
                selected_by_key[key] = latest_sig
            else:
                selected_by_key[key] = sorted(candidates)[0]

        creates = 0
        updates = 0
        unchanged = 0
        for key, signature in selected_by_key.items():
            prev = existing_by_key.get(key)
            if prev is None:
                creates += 1
            elif prev != signature:
                updates += 1
            else:
                unchanged += 1

        print(f"invoices_scanned={scanned}")
        print(f"invoices_with_sender={with_sender}")
        print(f"invoices_with_existing_file={with_existing_file}")
        print(f"signature_detected_rows={detected_signature_rows}")
        print(f"sender_keys_with_signature={len(selected_by_key)}")
        print(f"to_create={creates}")
        print(f"to_update={updates}")
        print(f"unchanged={unchanged}")

        if not apply:
            print("dry_run=true (no write)")
            return 0

        for (row_tenant_id, sender_email), signature in selected_by_key.items():
            _store_supplier_format(
                db=db,
                tenant_id=row_tenant_id,
                sender_email=sender_email,
                signature=signature,
            )
        db.commit()
        print("apply=true (writes committed)")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}")
        return 1
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill supplier_invoice_formats from historical inbound invoices "
            "using global signature detection."
        )
    )
    parser.add_argument("--tenant-id", type=int, default=None, help="Limit backfill to one tenant.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply writes (default is dry-run).",
    )
    args = parser.parse_args()
    raise SystemExit(run(tenant_id=args.tenant_id, apply=args.apply))


if __name__ == "__main__":
    main()
