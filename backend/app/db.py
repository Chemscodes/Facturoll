import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config

logger = logging.getLogger("app")


def _get_connect_args() -> dict:
    if config.DB_URL.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def _create_engine():
    if config.DB_URL.startswith("sqlite"):
        if config.DB_URL.endswith(":memory:"):
            return create_engine(
                config.DB_URL,
                connect_args=_get_connect_args(),
                poolclass=StaticPool,
            )
        return create_engine(config.DB_URL, connect_args=_get_connect_args())
    return create_engine(config.DB_URL)


engine = _create_engine()


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    if not config.DB_URL.startswith("sqlite"):
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        pass


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models

    if config.DB_URL.startswith("sqlite"):
        models.Base.metadata.create_all(bind=engine)
        _migrate_sqlite()
    elif config.DB_URL.startswith("postgresql"):
        _init_postgres_schema(models)
    else:
        models.Base.metadata.create_all(bind=engine)


def _init_postgres_schema(models) -> None:
    mode = str(getattr(config, "DB_SCHEMA_MODE", "legacy") or "legacy").strip().lower()
    if mode in {"legacy", "legacy_create_all", "auto"}:
        models.Base.metadata.create_all(bind=engine)
        _migrate_postgres()
        logger.info("DB schema mode=legacy (create_all + compat migrations)")
        return

    if mode in {"alembic_strict", "strict"}:
        ok, detail = _is_postgres_alembic_at_head()
        if not ok:
            raise RuntimeError(
                "Database schema is not at Alembic head. "
                f"detail={detail}. Run: alembic upgrade head"
            )
        logger.info("DB schema mode=alembic_strict (schema at head)")
        return

    raise RuntimeError(
        f"Unsupported APP_DB_SCHEMA_MODE='{mode}'. "
        "Use one of: legacy, alembic_strict."
    )


def _is_postgres_alembic_at_head() -> tuple[bool, str]:
    if not config.DB_URL.startswith("postgresql"):
        return True, "non_postgres"
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
    except Exception as exc:
        return False, f"alembic_import_failed:{type(exc).__name__}"

    try:
        alembic_cfg = AlembicConfig(str(config.BASE_DIR / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(config.BASE_DIR / "alembic"))
        script = ScriptDirectory.from_config(alembic_cfg)
        expected_heads = set(script.get_heads() or [])
        with engine.connect() as conn:
            if not _pg_table_exists(conn, "alembic_version"):
                return False, "missing_alembic_version_table"
            context = MigrationContext.configure(conn)
            current_heads = set(context.get_current_heads() or [])
        if current_heads == expected_heads:
            return True, f"heads={sorted(current_heads)}"
        return False, f"current={sorted(current_heads)} expected={sorted(expected_heads)}"
    except Exception as exc:
        return False, f"alembic_check_failed:{type(exc).__name__}:{exc}"


def _migrate_sqlite() -> None:
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for table_name in [
            "users",
            "suppliers",
            "groups",
            "group_members",
            "products",
            "campaigns",
            "campaign_items",
            "campaign_recipients",
            "responses",
            "response_items",
        ]:
            _add_tenant_column(conn, table_name)
        for table_name in ["suppliers", "groups", "products", "campaigns"]:
            _add_owner_column(conn, table_name)
        for table_name in ["campaigns", "email_jobs"]:
            _add_priority_column(conn, table_name)
        _add_campaign_metadata_columns(conn)
        _add_is_admin_column(conn)
        _migrate_settings_table(conn)
        _add_invoice_extraction_description(conn)
        _add_invoice_extraction_raw_description(conn)
        _add_invoice_similarity_meta(conn)
        _add_invoice_sender_phone(conn)
        _add_invoice_parse_meta(conn)
        _add_invoice_ocr_snapshot(conn)
        _add_product_meta(conn)
        _add_supplier_category(conn)
        _add_campaign_item_meta(conn)
        _add_tech_sheet_file_hash(conn)
        _ensure_sqlite_indexes(conn)


def _add_tenant_column(conn, table_name: str) -> None:
    if not _table_exists(conn, table_name):
        return
    if _column_exists(conn, table_name, "tenant_id"):
        return
    conn.exec_driver_sql(
        f'ALTER TABLE "{table_name}" ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1'
    )


def _add_owner_column(conn, table_name: str) -> None:
    if not _table_exists(conn, table_name):
        return
    if _column_exists(conn, table_name, "owner_user_id"):
        return
    conn.exec_driver_sql(
        f'ALTER TABLE "{table_name}" ADD COLUMN owner_user_id INTEGER NOT NULL DEFAULT 1'
    )


def _add_priority_column(conn, table_name: str) -> None:
    if not _table_exists(conn, table_name):
        return
    if _column_exists(conn, table_name, "priority"):
        return
    conn.exec_driver_sql(
        f'ALTER TABLE "{table_name}" ADD COLUMN priority INTEGER NOT NULL DEFAULT 0'
    )


def _add_campaign_metadata_columns(conn) -> None:
    if not _table_exists(conn, "campaigns"):
        return
    if not _column_exists(conn, "campaigns", "processing_mode"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN processing_mode VARCHAR(32)'
        )
    if not _column_exists(conn, "campaigns", "establishment_name"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN establishment_name VARCHAR(255)'
        )
    if not _column_exists(conn, "campaigns", "lot_reference"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN lot_reference VARCHAR(255)'
        )
    if not _column_exists(conn, "campaigns", "establishment_address"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN establishment_address TEXT'
        )
    if not _column_exists(conn, "campaigns", "establishment_phone"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN establishment_phone VARCHAR(50)'
        )
    if not _column_exists(conn, "campaigns", "pharmacy_service"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN pharmacy_service VARCHAR(255)'
        )
    if not _column_exists(conn, "campaigns", "market_office_service"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN market_office_service VARCHAR(255)'
        )
    if not _column_exists(conn, "campaigns", "establishment_type"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN establishment_type VARCHAR(255)'
        )
    if not _column_exists(conn, "campaigns", "deposit_date"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN deposit_date TIMESTAMP'
        )
    if not _column_exists(conn, "campaigns", "processing_in_progress"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN processing_in_progress INTEGER NOT NULL DEFAULT 0'
        )
    if not _column_exists(conn, "campaigns", "processing_validated"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaigns" ADD COLUMN processing_validated INTEGER NOT NULL DEFAULT 0'
        )


def _migrate_settings_table(conn) -> None:
    if not _table_exists(conn, "settings"):
        return
    if _column_exists(conn, "settings", "tenant_id"):
        return
    conn.exec_driver_sql('ALTER TABLE "settings" RENAME TO "settings_old"')
    conn.exec_driver_sql(
        """
        CREATE TABLE "settings" (
            id INTEGER PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            "key" VARCHAR(100) NOT NULL,
            value VARCHAR(500) NOT NULL,
            UNIQUE(tenant_id, "key")
        )
        """
    )
    conn.exec_driver_sql(
        'INSERT INTO "settings" (tenant_id, "key", value) '
        'SELECT 1, "key", value FROM "settings_old"'
    )
    conn.exec_driver_sql('DROP TABLE "settings_old"')


def _add_is_admin_column(conn) -> None:
    if not _table_exists(conn, "users"):
        return
    if _column_exists(conn, "users", "is_admin"):
        return
    conn.exec_driver_sql('ALTER TABLE "users" ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table_name},
    )
    return result.first() is not None


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    result = conn.exec_driver_sql(f'PRAGMA table_info("{table_name}")').all()
    return any(row[1] == column_name for row in result)


def _migrate_postgres() -> None:
    if not config.DB_URL.startswith("postgresql"):
        return
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        _add_pg_column(conn, "campaigns", "priority", "INTEGER NOT NULL DEFAULT 0")
        _add_pg_column(conn, "campaigns", "processing_mode", "VARCHAR(32)")
        _add_pg_column(conn, "campaigns", "establishment_name", "VARCHAR(255)")
        _add_pg_column(conn, "campaigns", "lot_reference", "VARCHAR(255)")
        _add_pg_column(conn, "campaigns", "establishment_address", "TEXT")
        _add_pg_column(conn, "campaigns", "establishment_phone", "VARCHAR(50)")
        _add_pg_column(conn, "campaigns", "pharmacy_service", "VARCHAR(255)")
        _add_pg_column(conn, "campaigns", "market_office_service", "VARCHAR(255)")
        _add_pg_column(conn, "campaigns", "establishment_type", "VARCHAR(255)")
        _add_pg_column(conn, "campaigns", "deposit_date", "TIMESTAMP")
        _add_pg_column(conn, "campaigns", "processing_in_progress", "BOOLEAN NOT NULL DEFAULT FALSE")
        _add_pg_column(conn, "campaigns", "processing_validated", "BOOLEAN NOT NULL DEFAULT FALSE")
        _add_pg_column(conn, "email_jobs", "priority", "INTEGER NOT NULL DEFAULT 0")
        _add_pg_column(conn, "invoice_extraction_items", "description", "TEXT")
        _add_pg_column(conn, "invoice_extraction_items", "raw_description", "TEXT")
        _add_pg_column(conn, "invoice_extraction_items", "similarity_score", "FLOAT")
        _add_pg_column(conn, "invoice_extraction_items", "similarity_threshold", "FLOAT")
        _add_pg_column(conn, "invoice_extraction_items", "similarity_points", "JSONB")
        _add_pg_column(conn, "invoice_extraction_items", "strict_components_payload", "JSONB")
        _add_pg_column(conn, "inbound_invoices", "sender_phone", "VARCHAR(50)")
        _add_pg_column(conn, "inbound_invoices", "parse_engine", "VARCHAR(50)")
        _add_pg_column(conn, "inbound_invoices", "parse_score", "FLOAT")
        _add_pg_column(conn, "inbound_invoices", "ocr_text_snapshot", "TEXT")
        _add_pg_column(conn, "campaign_attachments", "parse_engine", "VARCHAR(50)")
        _add_pg_column(conn, "campaign_attachments", "parse_score", "FLOAT")
        _add_pg_column(conn, "campaign_attachments", "extraction_preview", "JSONB")
        _add_pg_column(conn, "campaign_attachments", "strict_extraction_preview", "JSONB")
        _add_pg_column(conn, "campaign_attachments", "extracted_at", "TIMESTAMP")
        _add_pg_column(conn, "products", "description", "TEXT")
        _add_pg_column(conn, "products", "characteristics", "JSONB")
        _add_pg_column(conn, "products", "embedding_score", "FLOAT")
        _add_pg_column(conn, "campaign_items", "requested_description", "TEXT")
        _add_pg_column(conn, "campaign_items", "requested_characteristics", "JSONB")
        _add_pg_column(conn, "campaign_items", "requested_embedding_score", "FLOAT")
        _add_pg_column(conn, "campaign_items", "requested_qty_source", "VARCHAR(32)")
        _add_pg_column(conn, "campaign_items", "requested_qty_confidence", "FLOAT")
        _add_pg_column(conn, "tech_sheets", "file_hash", "VARCHAR(64)")
        _add_pg_column(conn, "suppliers", "supplier_category", "VARCHAR(120)")
        _ensure_pg_indexes(conn)


def _add_invoice_extraction_description(conn) -> None:
    if not _table_exists(conn, "invoice_extraction_items"):
        return
    if _column_exists(conn, "invoice_extraction_items", "description"):
        return
    conn.exec_driver_sql(
        'ALTER TABLE "invoice_extraction_items" ADD COLUMN description TEXT'
    )


def _add_invoice_extraction_raw_description(conn) -> None:
    if not _table_exists(conn, "invoice_extraction_items"):
        return
    if _column_exists(conn, "invoice_extraction_items", "raw_description"):
        return
    conn.exec_driver_sql(
        'ALTER TABLE "invoice_extraction_items" ADD COLUMN raw_description TEXT'
    )


def _add_invoice_similarity_meta(conn) -> None:
    if not _table_exists(conn, "invoice_extraction_items"):
        return
    if not _column_exists(conn, "invoice_extraction_items", "similarity_score"):
        conn.exec_driver_sql(
            'ALTER TABLE "invoice_extraction_items" ADD COLUMN similarity_score FLOAT'
        )
    if not _column_exists(conn, "invoice_extraction_items", "similarity_threshold"):
        conn.exec_driver_sql(
            'ALTER TABLE "invoice_extraction_items" ADD COLUMN similarity_threshold FLOAT'
        )
    if not _column_exists(conn, "invoice_extraction_items", "similarity_points"):
        conn.exec_driver_sql(
            'ALTER TABLE "invoice_extraction_items" ADD COLUMN similarity_points JSON'
        )


def _add_invoice_sender_phone(conn) -> None:
    if not _table_exists(conn, "inbound_invoices"):
        return
    if _column_exists(conn, "inbound_invoices", "sender_phone"):
        return
    conn.exec_driver_sql(
        'ALTER TABLE "inbound_invoices" ADD COLUMN sender_phone VARCHAR(50)'
    )


def _add_invoice_parse_meta(conn) -> None:
    if not _table_exists(conn, "inbound_invoices"):
        return
    if not _column_exists(conn, "inbound_invoices", "parse_engine"):
        conn.exec_driver_sql(
            'ALTER TABLE "inbound_invoices" ADD COLUMN parse_engine VARCHAR(50)'
        )
    if not _column_exists(conn, "inbound_invoices", "parse_score"):
        conn.exec_driver_sql(
            'ALTER TABLE "inbound_invoices" ADD COLUMN parse_score FLOAT'
        )


def _add_invoice_ocr_snapshot(conn) -> None:
    if not _table_exists(conn, "inbound_invoices"):
        return
    if _column_exists(conn, "inbound_invoices", "ocr_text_snapshot"):
        return
    conn.exec_driver_sql(
        'ALTER TABLE "inbound_invoices" ADD COLUMN ocr_text_snapshot TEXT'
    )


def _add_product_meta(conn) -> None:
    if not _table_exists(conn, "products"):
        return
    if not _column_exists(conn, "products", "description"):
        conn.exec_driver_sql('ALTER TABLE "products" ADD COLUMN description TEXT')
    if not _column_exists(conn, "products", "characteristics"):
        conn.exec_driver_sql('ALTER TABLE "products" ADD COLUMN characteristics JSON')
    if not _column_exists(conn, "products", "embedding_score"):
        conn.exec_driver_sql('ALTER TABLE "products" ADD COLUMN embedding_score FLOAT')


def _add_supplier_category(conn) -> None:
    if not _table_exists(conn, "suppliers"):
        return
    if _column_exists(conn, "suppliers", "supplier_category"):
        return
    conn.exec_driver_sql(
        'ALTER TABLE "suppliers" ADD COLUMN supplier_category VARCHAR(120)'
    )


def _add_campaign_item_meta(conn) -> None:
    if not _table_exists(conn, "campaign_items"):
        return
    if not _column_exists(conn, "campaign_items", "requested_description"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaign_items" ADD COLUMN requested_description TEXT'
        )
    if not _column_exists(conn, "campaign_items", "requested_characteristics"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaign_items" ADD COLUMN requested_characteristics JSON'
        )
    if not _column_exists(conn, "campaign_items", "requested_embedding_score"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaign_items" ADD COLUMN requested_embedding_score FLOAT'
        )
    if not _column_exists(conn, "campaign_items", "requested_qty_source"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaign_items" ADD COLUMN requested_qty_source VARCHAR(32)'
        )
    if not _column_exists(conn, "campaign_items", "requested_qty_confidence"):
        conn.exec_driver_sql(
            'ALTER TABLE "campaign_items" ADD COLUMN requested_qty_confidence FLOAT'
        )


def _add_tech_sheet_file_hash(conn) -> None:
    if not _table_exists(conn, "tech_sheets"):
        return
    if _column_exists(conn, "tech_sheets", "file_hash"):
        return
    conn.exec_driver_sql('ALTER TABLE "tech_sheets" ADD COLUMN file_hash VARCHAR(64)')


def _ensure_sqlite_indexes(conn) -> None:
    if _table_exists(conn, "email_jobs"):
        _create_sqlite_index(
            conn,
            "ix_email_jobs_queue_pick",
            "email_jobs",
            "status, next_attempt_at, priority, created_at",
        )
        _create_sqlite_index(
            conn,
            "ix_email_jobs_campaign_active",
            "email_jobs",
            "tenant_id, campaign_id, status, recipient_id",
        )
        _create_sqlite_index(
            conn,
            "ix_email_jobs_recipient_latest",
            "email_jobs",
            "tenant_id, recipient_id, created_at",
        )
    if _table_exists(conn, "embedding_jobs"):
        _create_sqlite_index(
            conn,
            "ix_embedding_jobs_queue_pick",
            "embedding_jobs",
            "status, next_attempt_at, created_at",
        )
        _create_sqlite_index(
            conn,
            "ix_embedding_jobs_processing_watchdog",
            "embedding_jobs",
            "status, updated_at",
        )
    if _table_exists(conn, "invoice_extraction_items"):
        _create_sqlite_index(
            conn,
            "ix_invoice_items_embedding_pending",
            "invoice_extraction_items",
            "status, notes, campaign_id, tenant_id",
        )
    if _table_exists(conn, "tech_sheets"):
        _create_sqlite_index(
            conn,
            "ix_tech_sheets_hash_scope",
            "tech_sheets",
            "tenant_id, campaign_id, file_hash",
        )


def _create_sqlite_index(conn, index_name: str, table_name: str, columns_sql: str) -> None:
    conn.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table_name}" ({columns_sql})'
    )


def _add_pg_column(conn, table_name: str, column_name: str, column_def: str) -> None:
    if not _pg_table_exists(conn, table_name):
        return
    if _pg_column_exists(conn, table_name, column_name):
        return
    conn.exec_driver_sql(f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} {column_def}')


def _pg_table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :name"
        ),
        {"name": table_name},
    )
    return result.first() is not None


def _pg_column_exists(conn, table_name: str, column_name: str) -> bool:
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table AND column_name = :column"
        ),
        {"table": table_name, "column": column_name},
    )
    return result.first() is not None


def _ensure_pg_indexes(conn) -> None:
    if _pg_table_exists(conn, "email_jobs"):
        _create_pg_index(
            conn,
            "ix_email_jobs_queue_pick",
            "email_jobs",
            "status, next_attempt_at, priority DESC, created_at ASC",
        )
        _create_pg_index(
            conn,
            "ix_email_jobs_campaign_active",
            "email_jobs",
            "tenant_id, campaign_id, status, recipient_id",
        )
        _create_pg_index(
            conn,
            "ix_email_jobs_recipient_latest",
            "email_jobs",
            "tenant_id, recipient_id, created_at DESC",
        )
    if _pg_table_exists(conn, "embedding_jobs"):
        _create_pg_index(
            conn,
            "ix_embedding_jobs_queue_pick",
            "embedding_jobs",
            "status, next_attempt_at, created_at",
        )
        _create_pg_index(
            conn,
            "ix_embedding_jobs_processing_watchdog",
            "embedding_jobs",
            "status, updated_at",
        )
    if _pg_table_exists(conn, "invoice_extraction_items"):
        _create_pg_index(
            conn,
            "ix_invoice_items_embedding_pending",
            "invoice_extraction_items",
            "status, notes, campaign_id, tenant_id",
        )
    if _pg_table_exists(conn, "tech_sheets"):
        _create_pg_index(
            conn,
            "ix_tech_sheets_hash_scope",
            "tech_sheets",
            "tenant_id, campaign_id, file_hash",
        )


def _create_pg_index(conn, index_name: str, table_name: str, columns_sql: str) -> None:
    conn.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table_name}" ({columns_sql})'
    )
