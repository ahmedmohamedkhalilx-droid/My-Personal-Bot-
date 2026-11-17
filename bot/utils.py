"""Small pure helpers (parsing, formatting). Kept free of I/O so they are easy to test."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import dateparser

from .config import Contacts

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")
TELEGRAM_LIMIT = 4000


def split_args(text: str, parts: int) -> list[str]:
    """Split command arguments on '|' into at most `parts` pieces.

    The last piece keeps any further '|' characters, so message bodies can contain them.
    """
    pieces = [p.strip() for p in text.split("|", parts - 1)]
    return pieces


def normalize_phone(raw: str) -> str:
    """WhatsApp Cloud API wants digits only, with country code (e.g. 201001234567)."""
    return re.sub(r"\D", "", raw)


@dataclass
class Recipients:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def add_email(self, e: str) -> None:
        if e and e.lower() not in (x.lower() for x in self.emails):
            self.emails.append(e)

    def add_phone(self, p: str) -> None:
        p = normalize_phone(p)
        if p and p not in self.phones:
            self.phones.append(p)


def resolve_recipients(spec: str, contacts: Contacts) -> Recipients:
    """Turn 'family, sara, x@y.com, +201001234567' into email addresses and phone numbers."""
    out = Recipients()
    tokens: list[str] = []
    for piece in re.split(r"[,;]+", spec):
        piece = piece.strip()
        # Pieces are comma separated; inside a piece, words split on spaces unless it's
        # a phone number written with spaces like "+20 100 123 4567".
        tokens.extend([piece] if PHONE_RE.match(piece) else piece.split())
    for token in tokens:
        if not token:
            continue
        key = token.lower()
        if key in contacts.groups:
            g = contacts.groups[key]
            for e in g["emails"]:
                out.add_email(e)
            for p in g["whatsapp"]:
                out.add_phone(p)
            for member in g.get("members", []):
                person = contacts.people.get(member)
                if person:
                    out.add_email(person["email"])
                    out.add_phone(person["whatsapp"])
        elif key in contacts.people:
            out.add_email(contacts.people[key]["email"])
            out.add_phone(contacts.people[key]["whatsapp"])
        elif EMAIL_RE.match(token):
            out.add_email(token)
        elif PHONE_RE.match(token):
            out.add_phone(token)
        else:
            out.unknown.append(token)
    return out


_DURATION_RE = re.compile(r"^\s*(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?|ins?)?)?\s*$", re.I)


def parse_duration(text: str | None, default_minutes: int = 60) -> timedelta | None:
    """'1h', '90m', '1h30m', '2 hours' -> timedelta. Returns None for 'all day'."""
    if not text:
        return timedelta(minutes=default_minutes)
    t = text.strip().lower()
    if t in ("allday", "all day", "all-day", "day"):
        return None
    if t.isdigit():
        return timedelta(minutes=int(t))
    m = _DURATION_RE.match(t)
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(f"Could not understand duration {text!r}. Try 1h, 90m or 'all day'.")
    hours = float(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    return timedelta(hours=hours, minutes=minutes)


def parse_when(text: str, tz: str, now: datetime | None = None) -> datetime:
    """Natural language date: 'tomorrow 3pm', 'next monday 10:00', '2026-12-01 14:30'."""
    settings = {
        "TIMEZONE": tz,
        "TO_TIMEZONE": tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    if now is not None:
        settings["RELATIVE_BASE"] = now.replace(tzinfo=None)
    dt = dateparser.parse(text, settings=settings)
    if dt is None:
        raise ValueError(f"Could not understand the date/time {text!r}. Try 'tomorrow 3pm' or '2026-12-01 14:30'.")
    return dt


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|h\d|tr)[^>]*>", re.I)


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = _BLOCK_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def truncate(text: str, limit: int = 300) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def esc(text: str | None) -> str:
    """Escape for Telegram HTML parse mode."""
    return html.escape(text or "", quote=False)


def chunk_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split long text on line boundaries so each piece fits in one Telegram message."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def matches_filters(sender: str, subject: str, body: str, filter_from: list[str], keywords: list[str]) -> bool:
    """No filters -> everything matches. Otherwise sender OR keyword must match."""
    if not filter_from and not keywords:
        return True
    sender_l = sender.lower()
    if any(f in sender_l for f in filter_from):
        return True
    haystack = f"{subject}\n{body}".lower()
    return any(k in haystack for k in keywords)
