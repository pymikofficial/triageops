"""Runtime configuration, read once from environment variables.

Every value here is server side only and is never sent to the browser.
Serverless instances are short lived, so reading at import time is enough.
Tests override individual attributes on ``settings`` with monkeypatch.
"""

import os
from dataclasses import dataclass

VERSION = "1.0.0"

DEFAULT_EMBED_URL = "https://sdrwlskpdnhjmynxgrnb.supabase.co/functions/v1/embed"


def _text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _integer(name: str, default: int) -> int:
    raw = _text(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    anthropic_api_key: str
    triage_model: str
    agent_model: str
    database_urls: list[str]
    embed_url: str
    embed_secret: str
    webhook_secret: str
    whatsapp_verify_token: str
    whatsapp_app_secret: str
    cron_secret: str
    mcp_token: str
    admin_token: str
    alert_webhook_url: str
    ip_hash_salt: str
    demo_hourly_limit: int
    demo_daily_limit: int
    chat_hourly_limit: int
    region: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            anthropic_api_key=_text("ANTHROPIC_API_KEY"),
            triage_model=_text("CLAUDE_TRIAGE_MODEL") or "claude-haiku-5-5",
            agent_model=_text("CLAUDE_AGENT_MODEL") or "claude-sonnet-5-5",
            database_urls=[url.strip() for url in _text("DATABASE_URLS").split(",") if url.strip()],
            embed_url=_text("EMBED_URL") or DEFAULT_EMBED_URL,
            embed_secret=_text("EMBED_SECRET"),
            webhook_secret=_text("WEBHOOK_SECRET"),
            whatsapp_verify_token=_text("WHATSAPP_VERIFY_TOKEN"),
            whatsapp_app_secret=_text("WHATSAPP_APP_SECRET"),
            cron_secret=_text("CRON_SECRET"),
            mcp_token=_text("MCP_TOKEN"),
            admin_token=_text("ADMIN_TOKEN"),
            alert_webhook_url=_text("ALERT_WEBHOOK_URL"),
            ip_hash_salt=_text("IP_HASH_SALT"),
            demo_hourly_limit=_integer("DEMO_HOURLY_LIMIT", 6),
            demo_daily_limit=_integer("DEMO_DAILY_LIMIT", 300),
            chat_hourly_limit=_integer("CHAT_HOURLY_LIMIT", 20),
            region=_text("VERCEL_REGION") or "local",
        )


settings = Settings.from_env()
