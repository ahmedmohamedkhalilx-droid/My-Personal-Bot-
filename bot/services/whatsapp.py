"""Send WhatsApp messages through Meta's official WhatsApp Cloud API.

WhatsApp rule to know: a business number may only send free-form text to someone who
messaged it in the last 24 hours. For everyone else you must use an approved *template*.
Set WHATSAPP_TEMPLATE_NAME to a template whose body is just "{{1}}" and the bot will put
your announcement text into that slot.
"""
from __future__ import annotations

import re

import httpx

from ..config import WhatsAppConfig


class WhatsAppSender:
    def __init__(self, cfg: WhatsAppConfig):
        self.cfg = cfg

    @property
    def _url(self) -> str:
        return f"https://graph.facebook.com/{self.cfg.api_version}/{self.cfg.phone_number_id}/messages"

    def _payload(self, to: str, text: str) -> dict:
        if self.cfg.template_name:
            # Template parameters may not contain newlines/tabs or 4+ consecutive spaces.
            param = re.sub(r"\s*\n\s*", " · ", text)
            param = re.sub(r"[\t ]{4,}", "   ", param)
            return {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": self.cfg.template_name,
                    "language": {"code": self.cfg.template_lang},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": param}]}],
                },
            }
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": True, "body": text},
        }

    async def send(self, phones: list[str], text: str) -> dict[str, str]:
        if not self.cfg.enabled:
            raise RuntimeError("WhatsApp is not configured (WHATSAPP_* in .env).")
        results: dict[str, str] = {}
        headers = {"Authorization": f"Bearer {self.cfg.token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            for phone in phones:
                try:
                    r = await client.post(self._url, headers=headers, json=self._payload(phone, text))
                    if r.status_code < 300:
                        results[phone] = "ok"
                    else:
                        err = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
                        results[phone] = err.get("message") or f"HTTP {r.status_code}"
                except httpx.HTTPError as exc:
                    results[phone] = str(exc)
        return results
