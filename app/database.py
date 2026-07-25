from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def migrate_schema(db_engine: Engine) -> None:
    """Small additive migration for databases created by the MVP.

    New installations already contain these columns via metadata. Existing
    SQLite/PostgreSQL installations are upgraded without deleting user data.
    """
    inspector = inspect(db_engine)
    tables = set(inspector.get_table_names())
    if not {"users", "payments", "subscriptions"}.issubset(tables):
        return
    dialect = db_engine.dialect.name
    datetime_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    additions = {
        "users": {
            "balance_units": "BIGINT NOT NULL DEFAULT 0",
        },
        "payments": {
            "requested_device_limit": "INTEGER NOT NULL DEFAULT 1",
        },
        "subscriptions": {
            "device_limit": "INTEGER NOT NULL DEFAULT 1",
            "last_billed_at": datetime_type,
        },
    }
    with db_engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    )
