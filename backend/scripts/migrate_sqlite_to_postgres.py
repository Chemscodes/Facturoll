import argparse
import sqlite3
from typing import Any, Iterable

from sqlalchemy import Boolean, create_engine, text

from app import config
from app.models import Base

TABLE_ORDER = [
    "tenants",
    "users",
    "email_templates",
    "suppliers",
    "groups",
    "group_members",
    "products",
    "campaigns",
    "campaign_items",
    "campaign_recipients",
    "email_jobs",
    "responses",
    "response_items",
    "suppressed_emails",
    "settings",
]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in rows]


def _default_owner_id(conn: sqlite3.Connection) -> int:
    columns = _sqlite_columns(conn, "users") if _table_exists(conn, "users") else []
    if "is_admin" in columns:
        row = conn.execute('SELECT id FROM "users" WHERE is_admin = 1 ORDER BY id LIMIT 1').fetchone()
        if row:
            return int(row[0])
    row = conn.execute('SELECT id FROM "users" ORDER BY id LIMIT 1').fetchone()
    return int(row[0]) if row else 1


def _normalize_bool(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
    return value


def _read_rows(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[dict[str, Any]]:
    if not columns:
        return []
    col_list = ", ".join([f'"{col}"' for col in columns])
    cursor = conn.execute(f'SELECT {col_list} FROM "{table}"')
    rows = []
    for row in cursor.fetchall():
        rows.append(dict(zip(columns, row)))
    return rows


def _set_sequences(pg_conn, table: str) -> None:
    pg_conn.execute(
        text(
            "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
            "COALESCE((SELECT MAX(id) FROM " + table + "), 1), "
            "true)"
        ),
        {"table_name": table},
    )


def _any_data(pg_conn, table: str) -> bool:
    result = pg_conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).fetchone()
    return result is not None


def _truncate_all(pg_conn, tables: Iterable[str]) -> None:
    joined = ", ".join(tables)
    pg_conn.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate SQLite data to PostgreSQL.")
    parser.add_argument("--sqlite", default=str(config.DB_PATH), help="Path to SQLite DB file.")
    parser.add_argument("--postgres", default=config.DB_URL, help="Postgres URL.")
    parser.add_argument("--wipe", action="store_true", help="Wipe target Postgres tables before insert.")
    args = parser.parse_args()

    if not args.postgres.startswith("postgresql"):
        print("APP_DB_URL must point to a PostgreSQL database.")
        return 1

    sqlite_conn = sqlite3.connect(args.sqlite)
    try:
        if not _table_exists(sqlite_conn, "users"):
            print("SQLite database looks empty or missing tables.")
            return 1

        owner_id = _default_owner_id(sqlite_conn)
        tenant_default = 1

        pg_engine = create_engine(args.postgres)
        Base.metadata.create_all(bind=pg_engine)

        with pg_engine.begin() as pg_conn:
            if not args.wipe:
                for table in TABLE_ORDER:
                    if _any_data(pg_conn, table):
                        print(f"Target table {table} is not empty. Use --wipe to overwrite.")
                        return 1
            else:
                _truncate_all(pg_conn, TABLE_ORDER)

            for table_name in TABLE_ORDER:
                if not _table_exists(sqlite_conn, table_name):
                    continue
                sqlite_cols = _sqlite_columns(sqlite_conn, table_name)
                table = Base.metadata.tables.get(table_name)
                if table is None:
                    continue
                target_cols = {col.name for col in table.columns}
                rows = _read_rows(sqlite_conn, table_name, sqlite_cols)
                if not rows:
                    continue

                bool_cols = {col.name for col in table.columns if isinstance(col.type, Boolean)}
                cleaned_rows = []
                for row in rows:
                    cleaned = {}
                    for key, value in row.items():
                        if key not in target_cols:
                            continue
                        cleaned[key] = _normalize_bool(value) if key in bool_cols else value

                    if "tenant_id" in target_cols and "tenant_id" not in cleaned:
                        cleaned["tenant_id"] = tenant_default
                    if "owner_user_id" in target_cols and "owner_user_id" not in cleaned:
                        cleaned["owner_user_id"] = owner_id
                    if "priority" in target_cols and "priority" not in cleaned:
                        cleaned["priority"] = 0

                    cleaned_rows.append(cleaned)

                if cleaned_rows:
                    pg_conn.execute(table.insert(), cleaned_rows)

            for table_name in TABLE_ORDER:
                table = Base.metadata.tables.get(table_name)
                if table is None or "id" not in table.c:
                    continue
                if _any_data(pg_conn, table_name):
                    _set_sequences(pg_conn, table_name)

        print("Migration completed.")
        return 0
    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
