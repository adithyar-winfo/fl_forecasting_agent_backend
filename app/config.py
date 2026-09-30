import sys
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_database_url(url: str) -> str:
    # Route generic PostgreSQL URLs to the installed psycopg v3 driver.
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _default_database_url() -> str:
    # Reuse existing project DB configuration if available.
    try:
        project_root = Path(__file__).resolve().parents[2]
        if str(project_root) not in sys.path:
            sys.path.append(str(project_root))
        from mcp_servers.db_mcp.config import config as db_config

        user = quote_plus(str(db_config["user"]))
        password = quote_plus(str(db_config["password"]))
        host = db_config["host"]
        port = db_config.get("port", 5432)
        dbname = db_config["dbname"]
        return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}"
    except Exception:
        return "sqlite:///./chat_backend.db"


class Settings(BaseSettings):
    app_name: str = "FL Agent Chat Backend"
    database_url: str = _default_database_url()
    database_schema: str = "fl"
    agent_base_url: str | None = None
    agent_chat_url: str | None = None
    agent_refresh: str | None = None
    agent_auth_header: str = "Authorization"
    agent_auth_token: str | None = None
    agent_timeout_seconds: float = 120.0

    model_config = SettingsConfigDict(
        env_prefix="CHAT_BACKEND_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
settings.database_url = _normalize_database_url(settings.database_url)
