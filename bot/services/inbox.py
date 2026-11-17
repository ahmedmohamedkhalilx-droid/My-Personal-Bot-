"""Read recent mail from your inboxes (read-only; nothing is marked as read).

- IMAP (Gmail, Yahoo, ...): use an app password. For Gmail: Google Account -> Security ->
  2-Step Verification -> App passwords. Host imap.gmail.com.
- Microsoft Graph (Outlook / Microsoft 365 university accounts): Microsoft has switched off
  password login for IMAP, so this uses a one-time device-code sign-in (/outlook_login in the
  bot). The token is cached and refreshed automatically.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import httpx
import msal

from ..config import InboxConfig
from ..utils import html_to_text

GRAPH_SCOPES = ["Mail.Read"]


@dataclass
class Mail:
    uid: str
    sender: str
    subject: str
    snippet: str
    received: datetime | None


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _plain_body(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    html_fallback = ""
    for part in parts:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            return text
        if part.get_content_type() == "text/html" and not html_fallback:
            html_fallback = html_to_text(text)
    return html_fallback


class ImapInbox:
    def __init__(self, cfg: InboxConfig):
        self.cfg = cfg

    def _fetch_sync(self, since_days: int, limit: int) -> list[Mail]:
        since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        conn = imaplib.IMAP4_SSL(self.cfg.host, self.cfg.port, timeout=30)
        try:
            conn.login(self.cfg.user, self.cfg.password)
            conn.select(self.cfg.folder, readonly=True)
            status, data = conn.uid("SEARCH", None, f"(SINCE {since})")
            if status != "OK":
                return []
            uids = data[0].split()[-limit:]
            mails = []
            for uid in uids:
                status, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                try:
                    received = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
                except (TypeError, ValueError):
                    received = None
                name, addr = parseaddr(_decode(msg.get("From")))
                mails.append(Mail(
                    uid=f"{self.cfg.folder}:{uid.decode()}",
                    sender=f"{name} <{addr}>" if name else addr,
                    subject=_decode(msg.get("Subject")) or "(no subject)",
                    snippet=_plain_body(msg)[:1000],
                    received=received,
                ))
            return mails
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    async def fetch_recent(self, since_days: int = 2, limit: int = 50) -> list[Mail]:
        return await asyncio.to_thread(self._fetch_sync, since_days, limit)


class GraphInbox:
    def __init__(self, cfg: InboxConfig, data_dir: Path):
        if not cfg.client_id:
            raise ValueError(f"{cfg.name}: graph inbox needs CLIENT_ID")
        self.cfg = cfg
        self._cache_path = data_dir / f"msal_{cfg.key}.json"
        self._cache = msal.SerializableTokenCache()
        if self._cache_path.exists():
            self._cache.deserialize(self._cache_path.read_text())
        self._app = msal.PublicClientApplication(
            cfg.client_id,
            authority=f"https://login.microsoftonline.com/{cfg.tenant}",
            token_cache=self._cache,
        )

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(self._cache.serialize())
            self._cache_path.chmod(0o600)

    def _token(self) -> str:
        accounts = self._app.get_accounts()
        result = self._app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0]) if accounts else None
        self._save_cache()
        if not result or "access_token" not in result:
            raise PermissionError(f"{self.cfg.name}: not signed in. Send /outlook_login to the bot.")
        return result["access_token"]

    @property
    def signed_in(self) -> bool:
        return bool(self._app.get_accounts())

    def start_device_login(self) -> dict:
        flow = self._app.initiate_device_flow(scopes=GRAPH_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start Microsoft sign-in: {json.dumps(flow)}")
        return flow

    def _finish_device_login_sync(self, flow: dict) -> dict:
        result = self._app.acquire_token_by_device_flow(flow)  # blocks until user finishes or it expires
        self._save_cache()
        return result

    async def finish_device_login(self, flow: dict) -> dict:
        return await asyncio.to_thread(self._finish_device_login_sync, flow)

    async def fetch_recent(self, since_days: int = 2, limit: int = 50) -> list[Mail]:
        token = await asyncio.to_thread(self._token)
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,bodyPreview,receivedDateTime",
            "$top": str(limit),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            items = r.json().get("value", [])
        mails = []
        for m in reversed(items):  # oldest first
            frm = (m.get("from") or {}).get("emailAddress") or {}
            name, addr = frm.get("name", ""), frm.get("address", "")
            received = m.get("receivedDateTime")
            mails.append(Mail(
                uid=m["id"],
                sender=f"{name} <{addr}>" if name else addr,
                subject=m.get("subject") or "(no subject)",
                snippet=m.get("bodyPreview") or "",
                received=datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None,
            ))
        return mails


def build_inbox(cfg: InboxConfig, data_dir: Path):
    return ImapInbox(cfg) if cfg.kind == "imap" else GraphInbox(cfg, data_dir)
