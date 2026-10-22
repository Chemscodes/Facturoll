import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def _parse_db_url(db_url: str) -> dict:
    normalized = db_url.replace("postgresql+psycopg2://", "postgresql://")
    parsed = urlparse(normalized)
    return {
        "user": parsed.username or "",
        "password": parsed.password or "",
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "database": (parsed.path or "").lstrip("/"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup Postgres using pg_dump.")
    parser.add_argument("--db-url", default=os.getenv("APP_DB_URL", ""), help="Postgres URL.")
    parser.add_argument("--output", default="backups", help="Output directory.")
    parser.add_argument("--pg-dump", default=os.getenv("PG_DUMP_PATH", "pg_dump"), help="Path to pg_dump.")
    args = parser.parse_args()

    if not args.db_url:
        print("APP_DB_URL is not set.")
        return 1

    info = _parse_db_url(args.db_url)
    if not info["database"]:
        print("Invalid APP_DB_URL (missing database).")
        return 1

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{info['database']}_{timestamp}.sql"
    output_path = output_dir / filename

    env = os.environ.copy()
    if info["password"]:
        env["PGPASSWORD"] = info["password"]

    cmd = [
        args.pg_dump,
        "-h",
        info["host"],
        "-p",
        str(info["port"]),
        "-U",
        info["user"],
        "-f",
        str(output_path),
        info["database"],
    ]
    result = subprocess.run(cmd, env=env, check=False)
    if result.returncode != 0:
        print("pg_dump failed.")
        return result.returncode

    print(f"Backup created: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
