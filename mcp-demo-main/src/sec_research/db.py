"""PostgreSQL 連線與明確 migration 入口。"""

import os
from pathlib import Path

import psycopg
from pydantic import SecretStr


INITIAL_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_initial.sql"


def apply_migrations(
    database_url: str | SecretStr,
    migration_path: Path = INITIAL_MIGRATION,
) -> None:
    """在單一交易中套用可重跑的 schema migration。"""
    dsn = (
        database_url.get_secret_value()
        if isinstance(database_url, SecretStr)
        else database_url
    )
    migration = migration_path.read_text(encoding="utf-8")
    with psycopg.connect(dsn) as connection:
        connection.execute(migration)


def main() -> None:
    """由環境變數明確執行 migration。"""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("缺少 DATABASE_URL，未執行 migration")
    apply_migrations(SecretStr(database_url))


if __name__ == "__main__":
    main()
