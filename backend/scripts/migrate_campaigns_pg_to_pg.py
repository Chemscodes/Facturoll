from __future__ import annotations

import argparse
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_campaign_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in (raw or "").split(","):
        token = chunk.strip()
        if not token:
            continue
        ids.append(int(token))
    unique = sorted(set(ids))
    if not unique:
        raise ValueError("campaign_ids is empty")
    return unique


def _fetch_rows(conn, table, id_values: set[int]) -> list[dict[str, Any]]:
    if not id_values:
        return []
    stmt = select(table).where(table.c.id.in_(sorted(id_values)))
    return [dict(row) for row in conn.execute(stmt).mappings().all()]


def _fetch_rows_by_campaign(conn, table, campaign_ids: list[int]) -> list[dict[str, Any]]:
    stmt = select(table).where(table.c.campaign_id.in_(campaign_ids))
    return [dict(row) for row in conn.execute(stmt).mappings().all()]


def _set_sequence(conn, table_name: str) -> None:
    conn.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table_name}), 1), true)"
        )
    )


def _upsert_rows(conn, table, rows: list[dict[str, Any]], dry_run: bool) -> int:
    if not rows:
        return 0
    if dry_run:
        return len(rows)
    stmt = pg_insert(table).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
    conn.execute(stmt)
    return len(rows)


def main() -> int:
    _load_env()

    parser = argparse.ArgumentParser(description="Migrate selected campaigns from one Postgres DB to another.")
    parser.add_argument("--campaign-ids", required=True, help="Comma-separated campaign IDs. Example: 54,55,56")
    parser.add_argument(
        "--source-db-url",
        default=(os.getenv("APP_DB_URL") or "").strip(),
        help="Source PostgreSQL URL (default: APP_DB_URL from backend/.env)",
    )
    parser.add_argument(
        "--target-db-url",
        required=True,
        help="Target PostgreSQL URL (example: postgresql+psycopg://user:pass@127.0.0.1:15432/saas_db)",
    )
    parser.add_argument(
        "--allow-existing-campaign-ids",
        action="store_true",
        help="Allow migration even if campaign IDs already exist on target (unsafe).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be copied.")
    args = parser.parse_args()

    campaign_ids = _parse_campaign_ids(args.campaign_ids)
    source_url = (args.source_db_url or "").strip()
    target_url = (args.target_db_url or "").strip()

    if not source_url.startswith("postgresql"):
        raise RuntimeError("source_db_url must be a PostgreSQL URL")
    if not target_url.startswith("postgresql"):
        raise RuntimeError("target_db_url must be a PostgreSQL URL")

    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url)

    metadata = MetaData()
    table_names = [
        "tenants",
        "users",
        "suppliers",
        "products",
        "campaigns",
        "campaign_attachments",
        "campaign_items",
        "campaign_recipients",
        "responses",
        "response_items",
        "inbound_invoices",
        "invoice_extraction_items",
        "tech_sheets",
        "tech_sheet_links",
        "invoice_item_characteristic_matches",
        "invoice_item_offer_analysis_cache",
        "offer_analysis_results",
        "campaign_results_cache",
    ]
    metadata.reflect(bind=source_engine, only=table_names)

    t = metadata.tables
    required = [
        "campaigns",
        "campaign_items",
        "campaign_recipients",
        "responses",
        "response_items",
        "inbound_invoices",
        "invoice_extraction_items",
        "tech_sheets",
        "tech_sheet_links",
        "invoice_item_characteristic_matches",
        "campaign_attachments",
    ]
    for name in required:
        if name not in t:
            raise RuntimeError(f"Missing table in source DB metadata: {name}")

    with source_engine.connect() as sconn:
        campaign_rows = _fetch_rows(sconn, t["campaigns"], set(campaign_ids))
        if not campaign_rows:
            raise RuntimeError(f"No campaigns found for ids={campaign_ids}")

        tenant_ids = {int(r["tenant_id"]) for r in campaign_rows}
        user_ids = {int(r["owner_user_id"]) for r in campaign_rows}
        supplier_ids: set[int] = set()
        product_ids: set[int] = set()

        rows_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        rows_by_table["campaigns"] = campaign_rows
        rows_by_table["campaign_attachments"] = _fetch_rows_by_campaign(sconn, t["campaign_attachments"], campaign_ids)

        campaign_items = _fetch_rows_by_campaign(sconn, t["campaign_items"], campaign_ids)
        rows_by_table["campaign_items"] = campaign_items
        product_ids.update(int(r["product_id"]) for r in campaign_items if r.get("product_id") is not None)

        campaign_recipients = _fetch_rows_by_campaign(sconn, t["campaign_recipients"], campaign_ids)
        rows_by_table["campaign_recipients"] = campaign_recipients
        supplier_ids.update(int(r["supplier_id"]) for r in campaign_recipients if r.get("supplier_id") is not None)

        responses = _fetch_rows_by_campaign(sconn, t["responses"], campaign_ids)
        rows_by_table["responses"] = responses
        response_ids = {int(r["id"]) for r in responses}
        supplier_ids.update(int(r["supplier_id"]) for r in responses if r.get("supplier_id") is not None)

        if response_ids:
            response_items_stmt = select(t["response_items"]).where(t["response_items"].c.response_id.in_(sorted(response_ids)))
            response_items = [dict(row) for row in sconn.execute(response_items_stmt).mappings().all()]
        else:
            response_items = []
        rows_by_table["response_items"] = response_items
        product_ids.update(int(r["product_id"]) for r in response_items if r.get("product_id") is not None)

        inbound_invoices = _fetch_rows_by_campaign(sconn, t["inbound_invoices"], campaign_ids)
        rows_by_table["inbound_invoices"] = inbound_invoices
        invoice_ids = {int(r["id"]) for r in inbound_invoices}

        invoice_items = _fetch_rows_by_campaign(sconn, t["invoice_extraction_items"], campaign_ids)
        rows_by_table["invoice_extraction_items"] = invoice_items
        invoice_item_ids = {int(r["id"]) for r in invoice_items}
        supplier_ids.update(int(r["supplier_id"]) for r in invoice_items if r.get("supplier_id") is not None)

        tech_sheets = _fetch_rows_by_campaign(sconn, t["tech_sheets"], campaign_ids)
        rows_by_table["tech_sheets"] = tech_sheets
        tech_sheet_ids = {int(r["id"]) for r in tech_sheets}
        supplier_ids.update(int(r["supplier_id"]) for r in tech_sheets if r.get("supplier_id") is not None)

        rows_by_table["tech_sheet_links"] = _fetch_rows_by_campaign(sconn, t["tech_sheet_links"], campaign_ids)
        rows_by_table["invoice_item_characteristic_matches"] = _fetch_rows_by_campaign(
            sconn, t["invoice_item_characteristic_matches"], campaign_ids
        )
        rows_by_table["invoice_item_offer_analysis_cache"] = _fetch_rows_by_campaign(
            sconn, t["invoice_item_offer_analysis_cache"], campaign_ids
        )
        rows_by_table["offer_analysis_results"] = _fetch_rows_by_campaign(
            sconn, t["offer_analysis_results"], campaign_ids
        )
        rows_by_table["campaign_results_cache"] = _fetch_rows_by_campaign(
            sconn, t["campaign_results_cache"], campaign_ids
        )
        user_ids.update(int(r["user_id"]) for r in rows_by_table["campaign_results_cache"] if r.get("user_id") is not None)

        tenants = _fetch_rows(sconn, t["tenants"], tenant_ids)
        rows_by_table["tenants"] = tenants

        users = _fetch_rows(sconn, t["users"], user_ids)
        rows_by_table["users"] = users

        suppliers = _fetch_rows(sconn, t["suppliers"], supplier_ids)
        rows_by_table["suppliers"] = suppliers
        user_ids.update(int(r["owner_user_id"]) for r in suppliers if r.get("owner_user_id") is not None)
        if user_ids:
            rows_by_table["users"] = _fetch_rows(sconn, t["users"], user_ids)

        products = _fetch_rows(sconn, t["products"], product_ids)
        rows_by_table["products"] = products
        user_ids.update(int(r["owner_user_id"]) for r in products if r.get("owner_user_id") is not None)
        if user_ids:
            rows_by_table["users"] = _fetch_rows(sconn, t["users"], user_ids)

    insert_order = [
        "tenants",
        "users",
        "suppliers",
        "products",
        "campaigns",
        "campaign_attachments",
        "campaign_items",
        "campaign_recipients",
        "responses",
        "response_items",
        "inbound_invoices",
        "invoice_extraction_items",
        "tech_sheets",
        "tech_sheet_links",
        "invoice_item_characteristic_matches",
        "invoice_item_offer_analysis_cache",
        "offer_analysis_results",
        "campaign_results_cache",
    ]

    print(f"Campaign IDs: {campaign_ids}")
    for name in insert_order:
        print(f"{name}: {len(rows_by_table.get(name, []))} row(s)")

    if args.dry_run:
        print("Dry run complete. No changes applied.")
        return 0

    inserted_counts: dict[str, int] = {}
    with target_engine.begin() as tconn:
        target_meta = MetaData()
        target_meta.reflect(bind=tconn, only=insert_order)
        target_campaigns = target_meta.tables["campaigns"]
        existing_campaign_rows = tconn.execute(
            select(target_campaigns.c.id, target_campaigns.c.title).where(
                target_campaigns.c.id.in_(campaign_ids)
            )
        ).all()
        if existing_campaign_rows and not args.allow_existing_campaign_ids:
            existing = ", ".join(f"{row.id}:{row.title}" for row in existing_campaign_rows)
            raise RuntimeError(
                "Target already contains campaign IDs. "
                f"Refusing to continue to prevent data corruption: {existing}"
            )
        for name in insert_order:
            table = target_meta.tables[name]
            inserted_counts[name] = _upsert_rows(
                tconn,
                table,
                rows_by_table.get(name, []),
                dry_run=False,
            )
        for name in insert_order:
            table = target_meta.tables[name]
            if "id" in table.c:
                _set_sequence(tconn, name)

    print("Migration complete.")
    for name in insert_order:
        print(f"{name}: attempted {inserted_counts.get(name, 0)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
