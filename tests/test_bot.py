"""Checks the 'only notify about new things' logic with fake Canvas/inbox services."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bot.main import Bot, build_app
from bot.services.canvas import Announcement, Assignment
from bot.services.inbox import Mail


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_OWNER_IDS", "42")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CANVAS_BASE_URL", "https://canvas.example.com")
    monkeypatch.setenv("CANVAS_TOKEN", "t")
    monkeypatch.setenv("INBOX1_TYPE", "imap")
    monkeypatch.setenv("INBOX1_NAME", "Gmail")
    monkeypatch.setenv("INBOX1_FILTER_KEYWORDS", "announcement")
    monkeypatch.setenv("INBOX2_TYPE", "")
    from bot.config import load_config
    b = Bot(load_config())
    b.sent = []

    async def fake_notify(app, text, markup=None):
        b.sent.append(text)

    b.notify = fake_notify
    return b


def _asg(i, hours):
    return Assignment(id=i, course="CSCE 101", name=f"HW{i}", due_at=datetime.now(timezone.utc) + timedelta(hours=hours),
                      url="u", submitted=False, points=10)


def test_canvas_first_run_is_silent_then_reports_new(bot):
    anns = [Announcement(1, "CSCE", "Welcome", "<p>hi</p>", None, "Dr X", "u")]
    asgs = [_asg(1, 100)]

    async def fake_ann(days):
        return anns

    async def fake_asg(days):
        return asgs

    bot.canvas.recent_announcements = fake_ann
    bot.canvas.upcoming_assignments = fake_asg

    assert asyncio.run(bot.check_canvas(None)) == 0          # baseline, nothing sent
    anns.append(Announcement(2, "CSCE", "Quiz moved", "<p>Now Monday</p>", None, "Dr X", "u"))
    asgs.append(_asg(2, 10))                                   # new and due within 24h
    assert asyncio.run(bot.check_canvas(None)) == 3
    assert any("Quiz moved" in s for s in bot.sent)
    assert any("New assignment" in s and "HW2" in s for s in bot.sent)
    assert any("Due within 24 hours" in s for s in bot.sent)
    assert asyncio.run(bot.check_canvas(None)) == 0          # nothing repeated


def test_inbox_filters_and_dedupes(bot):
    mails = [Mail("1", "a@b.com", "old", "", None)]
    ic, inbox = bot.inboxes[0]

    async def fake_fetch(since_days=2, limit=50):
        return mails

    inbox.fetch_recent = fake_fetch
    assert asyncio.run(bot.check_inboxes(None)) == (0, [])
    mails += [Mail("2", "a@b.com", "Lunch?", "", None), Mail("3", "dean@u.edu", "Announcement: holiday", "", None)]
    assert asyncio.run(bot.check_inboxes(None)) == (1, [])
    assert "holiday" in bot.sent[0]
    assert asyncio.run(bot.check_inboxes(None)) == (0, [])


def test_build_app_registers_handlers_and_jobs(bot):
    app = build_app(bot.cfg)
    names = {j.name for j in app.job_queue.jobs()}
    assert {"canvas", "inbox", "digest"} <= names


def test_announce_requires_confirmation_then_sends(bot, tmp_path, monkeypatch):
    (tmp_path / "c.yaml").write_text("groups:\n  team:\n    emails: [a@x.com]\n    whatsapp: ['+201001234567']\n")
    bot.cfg.contacts_file = tmp_path / "c.yaml"
    bot.cfg.smtp.host, bot.cfg.smtp.user, bot.cfg.smtp.password = "smtp", "u", "p"
    bot.cfg.whatsapp.token, bot.cfg.whatsapp.phone_number_id = "t", "1"
    replies, calls = [], []

    async def fake_reply(update, text, markup=None):
        replies.append((text, markup))

    async def fake_email(rcpts, subject, body):
        calls.append(("email", rcpts, subject, body))
        return {r: "ok" for r in rcpts}

    async def fake_wa(phones, body):
        calls.append(("wa", phones, body))
        return {p: "ok" for p in phones}

    bot.reply, bot.mailer.send, bot.wa.send = fake_reply, fake_email, fake_wa
    asyncio.run(bot._prepare_send(None, "team | Meeting | See you at 5", "announce"))
    assert calls == []                                    # nothing sent before confirming
    text, markup = replies[-1]
    assert "a@x.com" in text and "+201001234567" in text
    data = markup.inline_keyboard[0][0].callback_data

    class Q:
        def __init__(self):
            self.data = data
        async def answer(self): pass
        async def edit_message_reply_markup(self, m): pass

    class U:
        callback_query = Q()

    asyncio.run(bot.on_send_button(U(), None))
    assert calls == [("email", ["a@x.com"], "Meeting", "See you at 5"), ("wa", ["201001234567"], "See you at 5")]
    assert "1/1 sent" in replies[-1][0]
