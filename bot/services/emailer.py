"""Send email announcements over SMTP (Gmail app password, Outlook, any SMTP server)."""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from ..config import SmtpConfig


class EmailSender:
    def __init__(self, cfg: SmtpConfig):
        self.cfg = cfg

    def _send_sync(self, recipients: list[str], subject: str, body: str) -> dict[str, str]:
        """Sends one message per recipient so nobody sees the others' addresses.

        Returns {address: "ok" | error text}.
        """
        results: dict[str, str] = {}
        context = ssl.create_default_context()
        if self.cfg.port == 465:
            server = smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=30)
            server.starttls(context=context)
        sender = formataddr((self.cfg.from_name, self.cfg.from_addr)) if self.cfg.from_name else self.cfg.from_addr
        try:
            server.login(self.cfg.user, self.cfg.password)
            for rcpt in recipients:
                msg = EmailMessage()
                msg["From"] = sender
                msg["To"] = rcpt
                msg["Subject"] = subject
                msg["Message-ID"] = make_msgid()
                msg.set_content(body)
                try:
                    server.send_message(msg)
                    results[rcpt] = "ok"
                except smtplib.SMTPException as exc:
                    results[rcpt] = str(exc)
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
        return results

    async def send(self, recipients: list[str], subject: str, body: str) -> dict[str, str]:
        if not self.cfg.enabled:
            raise RuntimeError("Email sending is not configured (SMTP_* in .env).")
        return await asyncio.to_thread(self._send_sync, recipients, subject, body)
