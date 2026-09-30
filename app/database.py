from collections.abc import Generator
import re

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


def _resolved_schema(database_url: str, configured_schema: str) -> str | None:
    # SQLite does not support named schemas like PostgreSQL.
    if database_url.startswith("sqlite"):
        return None
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", configured_schema):
        raise ValueError(f"Invalid CHAT_BACKEND_DATABASE_SCHEMA value: {configured_schema!r}")
    return configured_schema


DB_SCHEMA = _resolved_schema(settings.database_url, settings.database_schema)
metadata_obj = MetaData(schema=DB_SCHEMA) if DB_SCHEMA else MetaData()


class Base(DeclarativeBase):
    metadata = metadata_obj


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def initialize_database() -> None:
    if DB_SCHEMA:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
