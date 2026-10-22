import argparse
from typing import Any

from app import config
from app.db import SessionLocal
from app.models import CampaignAttachment
from app.services.campaign_attachment_extraction import extract_attachment_products_preview


def _summarize(preview: dict[str, Any]) -> dict[str, Any]:
    products = preview.get("products") or []
    unassigned = preview.get("unassigned") or []
    return {
        "doc_type": preview.get("doc_type"),
        "products": len(products) if isinstance(products, list) else 0,
        "unassigned": len(unassigned) if isinstance(unassigned, list) else 0,
        "engine": preview.get("engine"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--tenant", type=int, default=None)
    parser.add_argument("--campaign", type=int, default=None)
    parser.add_argument("--doc-hint", type=str, default=None)
    parser.add_argument("--layout", type=str, default=None)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = db.query(CampaignAttachment).order_by(CampaignAttachment.created_at.desc())
        if args.tenant is not None:
            q = q.filter(CampaignAttachment.tenant_id == args.tenant)
        if args.campaign is not None:
            q = q.filter(CampaignAttachment.campaign_id == args.campaign)
        rows = q.limit(args.limit).all()
    finally:
        db.close()

    if not rows:
        print("No attachments found.")
        return

    original_mode = getattr(config, "OPENAI_ATTACHMENT_EXTRACTION_MODE", "heuristic")
    modes = ["heuristic", "llm_structured"]

    for row in rows:
        print(f"\n=== {row.id} | {row.file_name} ===")
        for mode in modes:
            setattr(config, "OPENAI_ATTACHMENT_EXTRACTION_MODE", mode)
            preview = extract_attachment_products_preview(
                row.file_path,
                None,
                None,
                False,
                args.doc_hint,
                args.layout,
                None,
                False,
            )
            summary = _summarize(preview)
            print(f"{mode}: doc_type={summary['doc_type']} products={summary['products']} unassigned={summary['unassigned']} engine={summary['engine']}")

    setattr(config, "OPENAI_ATTACHMENT_EXTRACTION_MODE", original_mode)


if __name__ == "__main__":
    main()
