"""Canvas LMS: upcoming assignments and course announcements via the REST API.

Get a token in Canvas: Account -> Settings -> "+ New Access Token".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..config import CanvasConfig


@dataclass
class Assignment:
    id: int
    course: str
    name: str
    due_at: datetime | None
    url: str
    submitted: bool
    points: float | None


@dataclass
class Announcement:
    id: int
    course: str
    title: str
    message_html: str
    posted_at: datetime | None
    author: str
    url: str


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CanvasClient:
    def __init__(self, cfg: CanvasConfig):
        self.cfg = cfg

    def _client(self) -> httpx.AsyncClient:
        if not self.cfg.enabled:
            raise RuntimeError("Canvas is not configured (CANVAS_* in .env).")
        return httpx.AsyncClient(
            base_url=self.cfg.base_url,
            headers={"Authorization": f"Bearer {self.cfg.token}"},
            timeout=30,
            follow_redirects=True,
        )

    @staticmethod
    async def _get_all(client: httpx.AsyncClient, url: str, params=None) -> list:
        """Follow Canvas' Link-header pagination."""
        items: list = []
        next_url: str | None = url
        while next_url:
            r = await client.get(next_url, params=params)
            r.raise_for_status()
            items.extend(r.json())
            next_url = r.links.get("next", {}).get("url")
            params = None  # the next link already carries the query string
        return items

    async def _courses(self, client: httpx.AsyncClient) -> dict[int, str]:
        courses = await self._get_all(
            client, "/api/v1/courses", params={"enrollment_state": "active", "per_page": 100}
        )
        return {
            c["id"]: c.get("course_code") or c.get("name") or str(c["id"])
            for c in courses
            if not c.get("access_restricted_by_date")
        }

    async def upcoming_assignments(self, days: int = 14, include_submitted: bool = False) -> list[Assignment]:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=days)
        out: list[Assignment] = []
        async with self._client() as client:
            courses = await self._courses(client)
            for cid, cname in courses.items():
                try:
                    raw = await self._get_all(
                        client,
                        f"/api/v1/courses/{cid}/assignments",
                        params={"bucket": "future", "include[]": "submission", "order_by": "due_at", "per_page": 100},
                    )
                except httpx.HTTPStatusError:
                    continue  # some courses hide assignments; skip them
                for a in raw:
                    due = _dt(a.get("due_at"))
                    if due is None or not (now <= due <= horizon):
                        continue
                    sub = a.get("submission") or {}
                    submitted = sub.get("workflow_state") in ("submitted", "graded", "pending_review") or bool(sub.get("submitted_at"))
                    if submitted and not include_submitted:
                        continue
                    out.append(Assignment(
                        id=a["id"], course=cname, name=a.get("name", "Untitled"), due_at=due,
                        url=a.get("html_url", ""), submitted=submitted, points=a.get("points_possible"),
                    ))
        out.sort(key=lambda x: x.due_at or horizon)
        return out

    async def recent_announcements(self, days: int = 14) -> list[Announcement]:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        async with self._client() as client:
            courses = await self._courses(client)
            if not courses:
                return []
            params = [("context_codes[]", f"course_{cid}") for cid in courses]
            params += [("start_date", start), ("per_page", "50")]
            raw = await self._get_all(client, "/api/v1/announcements", params=params)
        out = []
        for a in raw:
            cid = int(str(a.get("context_code", "course_0")).split("_")[-1])
            out.append(Announcement(
                id=a["id"],
                course=courses.get(cid, str(cid)),
                title=a.get("title", ""),
                message_html=a.get("message", ""),
                posted_at=_dt(a.get("posted_at")),
                author=(a.get("author") or {}).get("display_name", ""),
                url=a.get("html_url", ""),
            ))
        out.sort(key=lambda x: x.posted_at or datetime.min.replace(tzinfo=timezone.utc))
        return out
