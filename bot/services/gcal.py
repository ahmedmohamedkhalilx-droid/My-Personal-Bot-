"""Google Calendar via a service account.

Setup: create a service account in Google Cloud, enable the Calendar API, download its JSON
key, then in Google Calendar share your calendar with the service account's email address
("Make changes to events").
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

from ..config import CalendarConfig

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarClient:
    def __init__(self, cfg: CalendarConfig, timezone: str):
        self.cfg = cfg
        self.timezone = timezone
        self._service = None

    def _svc(self):
        if self._service is None:
            if not self.cfg.enabled:
                raise RuntimeError("Google Calendar is not configured (GOOGLE_* in .env).")
            creds = service_account.Credentials.from_service_account_file(self.cfg.service_account_file, scopes=SCOPES)
            self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _add_sync(self, title: str, start: datetime, duration: timedelta | None,
                  location: str | None, description: str | None, reminder_minutes: list[int] | None) -> dict:
        if duration is None:  # all-day
            body = {
                "start": {"date": start.date().isoformat()},
                "end": {"date": (start.date() + timedelta(days=1)).isoformat()},
            }
        else:
            body = {
                "start": {"dateTime": start.isoformat(), "timeZone": self.timezone},
                "end": {"dateTime": (start + duration).isoformat(), "timeZone": self.timezone},
            }
        body["summary"] = title
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if reminder_minutes:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": m} for m in reminder_minutes],
            }
        return self._svc().events().insert(calendarId=self.cfg.calendar_id, body=body).execute()

    async def add_event(self, title: str, start: datetime, duration: timedelta | None,
                        location: str | None = None, description: str | None = None,
                        reminder_minutes: list[int] | None = None) -> dict:
        return await asyncio.to_thread(self._add_sync, title, start, duration, location, description, reminder_minutes)

    def _list_sync(self, start: datetime, end: datetime) -> list[dict]:
        res = self._svc().events().list(
            calendarId=self.cfg.calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        return res.get("items", [])

    async def list_events(self, start: datetime, end: datetime) -> list[dict]:
        return await asyncio.to_thread(self._list_sync, start, end)
