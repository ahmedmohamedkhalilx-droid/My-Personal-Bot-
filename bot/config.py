"""Loads all settings from environment variables (.env) and contacts.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


def _env_list(name: str) -> list[str]:
    value = _env(name, "") or ""
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    from_name: str | None

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.user and self.password)


@dataclass
class WhatsAppConfig:
    token: str | None
    phone_number_id: str | None
    template_name: str | None
    template_lang: str
    api_version: str

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.phone_number_id)


@dataclass
class CalendarConfig:
    service_account_file: str | None
    calendar_id: str | None

    @property
    def enabled(self) -> bool:
        return bool(self.service_account_file and self.calendar_id)


@dataclass
class CanvasConfig:
    base_url: str | None
    token: str | None
    poll_minutes: int

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)


@dataclass
class InboxConfig:
    """One mailbox to watch. kind is 'imap' (Gmail etc.) or 'graph' (Outlook / Microsoft 365)."""

    key: str
    name: str
    kind: str
    # IMAP
    host: str | None = None
    port: int = 993
    user: str | None = None
    password: str | None = None
    folder: str = "INBOX"
    # Microsoft Graph
    client_id: str | None = None
    tenant: str = "common"
    # Filters: if set, only matching mails are reported
    filter_from: list[str] = field(default_factory=list)
    filter_keywords: list[str] = field(default_factory=list)


@dataclass
class Config:
    telegram_token: str
    owner_ids: list[int]
    timezone: str
    data_dir: Path
    digest_time: str | None
    smtp: SmtpConfig
    whatsapp: WhatsAppConfig
    calendar: CalendarConfig
    canvas: CanvasConfig
    inboxes: list[InboxConfig]
    inbox_poll_minutes: int
    contacts_file: Path


def _load_inboxes() -> list[InboxConfig]:
    inboxes = []
    for n in range(1, 6):
        prefix = f"INBOX{n}_"
        kind = _env(prefix + "TYPE")
        if not kind:
            continue
        kind = kind.lower()
        if kind not in ("imap", "graph"):
            raise ValueError(f"{prefix}TYPE must be 'imap' or 'graph', got {kind!r}")
        inboxes.append(
            InboxConfig(
                key=f"inbox{n}",
                name=_env(prefix + "NAME", f"Inbox {n}"),
                kind=kind,
                host=_env(prefix + "HOST"),
                port=_env_int(prefix + "PORT", 993),
                user=_env(prefix + "USER"),
                password=_env(prefix + "PASSWORD"),
                folder=_env(prefix + "FOLDER", "INBOX"),
                client_id=_env(prefix + "CLIENT_ID"),
                tenant=_env(prefix + "TENANT", "common"),
                filter_from=[s.lower() for s in _env_list(prefix + "FILTER_FROM")],
                filter_keywords=[s.lower() for s in _env_list(prefix + "FILTER_KEYWORDS")],
            )
        )
    return inboxes


def load_config() -> Config:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    smtp_user = _env("SMTP_USER", "")
    return Config(
        telegram_token=token,
        owner_ids=[int(x) for x in _env_list("TELEGRAM_OWNER_IDS")],
        timezone=_env("TIMEZONE", "Africa/Cairo"),
        data_dir=Path(_env("DATA_DIR", "data")),
        digest_time=_env("DIGEST_TIME", "08:00"),
        smtp=SmtpConfig(
            host=_env("SMTP_HOST", ""),
            port=_env_int("SMTP_PORT", 587),
            user=smtp_user,
            password=_env("SMTP_PASSWORD", ""),
            from_addr=_env("SMTP_FROM", smtp_user),
            from_name=_env("SMTP_FROM_NAME"),
        ),
        whatsapp=WhatsAppConfig(
            token=_env("WHATSAPP_TOKEN"),
            phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
            template_name=_env("WHATSAPP_TEMPLATE_NAME"),
            template_lang=_env("WHATSAPP_TEMPLATE_LANG", "en"),
            api_version=_env("WHATSAPP_API_VERSION", "v21.0"),
        ),
        calendar=CalendarConfig(
            service_account_file=_env("GOOGLE_SERVICE_ACCOUNT_FILE"),
            calendar_id=_env("GOOGLE_CALENDAR_ID"),
        ),
        canvas=CanvasConfig(
            base_url=(_env("CANVAS_BASE_URL") or "").rstrip("/") or None,
            token=_env("CANVAS_TOKEN"),
            poll_minutes=_env_int("CANVAS_POLL_MINUTES", 30),
        ),
        inboxes=_load_inboxes(),
        inbox_poll_minutes=_env_int("INBOX_POLL_MINUTES", 10),
        contacts_file=Path(_env("CONTACTS_FILE", "contacts.yaml")),
    )


@dataclass
class Contacts:
    groups: dict[str, dict[str, list[str]]]
    people: dict[str, dict[str, str]]


def load_contacts(path: Path) -> Contacts:
    if not path.is_file():
        return Contacts(groups={}, people={})
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groups = {}
    for name, g in (raw.get("groups") or {}).items():
        g = g or {}
        groups[str(name).lower()] = {
            "emails": [str(e) for e in g.get("emails") or []],
            "whatsapp": [str(p) for p in g.get("whatsapp") or []],
            "members": [str(m).lower() for m in g.get("members") or []],
        }
    people = {}
    for name, p in (raw.get("contacts") or {}).items():
        p = p or {}
        people[str(name).lower()] = {
            "email": str(p["email"]) if p.get("email") else "",
            "whatsapp": str(p["whatsapp"]) if p.get("whatsapp") else "",
        }
    return Contacts(groups=groups, people=people)
