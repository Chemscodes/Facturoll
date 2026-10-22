from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.routes.admin import maintenance_cleanup


def main() -> int:
    parser = argparse.ArgumentParser(description="Run safe backend maintenance cleanup.")
    parser.add_argument("--dry-run", action="store_true", help="Only report, do not delete.")
    parser.add_argument("--tenant-id", type=int, default=None, help="Optional tenant scope.")
    parser.add_argument("--logs-days", type=int, default=30)
    parser.add_argument("--outbox-days", type=int, default=30)
    parser.add_argument("--email-jobs-days", type=int, default=90)
    parser.add_argument(
        "--json-out",
        type=str,
        default="",
        help="Optional file path to write JSON report.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = maintenance_cleanup(
            dry_run=bool(args.dry_run),
            tenant_id=args.tenant_id,
            logs_retention_days=args.logs_days,
            outbox_retention_days=args.outbox_days,
            email_jobs_retention_days=args.email_jobs_days,
            include_orphan_attachments=True,
            db=db,
        )
        report["ran_at_utc"] = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        print(payload)

        if args.json_out:
            out_path = Path(args.json_out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload, encoding="utf-8")
        return 0
    except Exception as exc:
        print(f"cleanup_failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
