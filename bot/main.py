"""Telegram personal assistant bot.

Jobs:
  1. Send email / WhatsApp announcements on command (/email, /whatsapp, /announce).
  2. Add things to Google Calendar (/event, /agenda).
  3. Watch Canvas (assignments + announcements) and two email inboxes, and notify you.
"""
from __future__ import annotations

import functools
import logging
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .config import Config, load_config, load_contacts
from .services.canvas import CanvasClient
from .services.emailer import EmailSender
from .services.gcal import CalendarClient
from .services.inbox import GraphInbox, build_inbox
from .services.whatsapp import WhatsAppSender
from .storage import Storage
from .utils import (
    chunk_message,
    esc,
    html_to_text,
    matches_filters,
    parse_duration,
    parse_when,
    resolve_recipients,
    split_args,
    truncate,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

HELP = """<b>Your personal bot</b>

<b>📣 Announcements</b> (you confirm before anything is sent)
/email <i>to</i> | <i>subject</i> | <i>message</i>
/whatsapp <i>to</i> | <i>message</i>
/announce <i>to</i> | <i>subject</i> | <i>message</i>  — email + WhatsApp
<i>to</i> = group or contact names from contacts.yaml, emails, or phone numbers, comma separated.
/contacts — list your groups and contacts

<b>📅 Calendar</b>
/event <i>title</i> | <i>when</i> [| <i>duration</i>] [| <i>location</i>]
   e.g. <code>/event Study group | tomorrow 6pm | 2h | Library</code>
   duration: 45m, 1h30m, all day (default 1h)
/agenda [days] — upcoming events (default 7 days)

<b>🎓 Canvas & email</b>
/assignments [days] — unsubmitted assignments due soon (default 14)
/canvas — check Canvas for new announcements/assignments now
/inbox — check your email inboxes now
/outlook_login — one-time sign-in for Outlook / Microsoft 365 inboxes

/status — what is configured
I also check automatically and send a morning digest."""


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.store = Storage(cfg.data_dir / "bot.db")
        self.mailer = EmailSender(cfg.smtp)
        self.wa = WhatsAppSender(cfg.whatsapp)
        self.cal = CalendarClient(cfg.calendar, cfg.timezone)
        self.canvas = CanvasClient(cfg.canvas)
        self.inboxes = [(ic, build_inbox(ic, cfg.data_dir)) for ic in cfg.inboxes]
        self.pending: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def contacts(self):
        return load_contacts(self.cfg.contacts_file)  # re-read every time so edits apply without restart

    async def notify(self, app: Application, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        for chat_id in self.cfg.owner_ids:
            chunks = chunk_message(text)
            for i, chunk in enumerate(chunks):
                await app.bot.send_message(
                    chat_id, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    reply_markup=markup if i == len(chunks) - 1 else None,
                )

    async def reply(self, update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        chunks = chunk_message(text)
        for i, chunk in enumerate(chunks):
            await update.effective_message.reply_text(
                chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_markup=markup if i == len(chunks) - 1 else None,
            )

    def fmt_dt(self, dt: datetime | None) -> str:
        if dt is None:
            return "no date"
        local = dt.astimezone(self.tz)
        return local.strftime("%a %d %b, %H:%M")

    def fmt_due(self, dt: datetime | None) -> str:
        if dt is None:
            return "no due date"
        delta = dt - datetime.now(self.tz)
        hours = delta.total_seconds() / 3600
        rel = f"in {int(hours)}h" if hours < 48 else f"in {int(hours // 24)}d"
        return f"{self.fmt_dt(dt)} ({rel})"

    # ------------------------------------------------------------------ basic commands
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, HELP)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def mark(ok: bool) -> str:
            return "✅" if ok else "❌"

        lines = [
            "<b>Status</b>",
            f"{mark(self.cfg.smtp.enabled)} Email sending (SMTP)",
            f"{mark(self.cfg.whatsapp.enabled)} WhatsApp"
            + (f" — template <code>{esc(self.cfg.whatsapp.template_name)}</code>" if self.cfg.whatsapp.template_name else " — free text (24h window only)"),
            f"{mark(self.cfg.calendar.enabled)} Google Calendar",
            f"{mark(self.cfg.canvas.enabled)} Canvas — every {self.cfg.canvas.poll_minutes} min",
        ]
        for ic, inbox in self.inboxes:
            extra = ""
            if isinstance(inbox, GraphInbox):
                extra = " (signed in)" if inbox.signed_in else " (needs /outlook_login)"
            lines.append(f"✅ Inbox <b>{esc(ic.name)}</b> [{ic.kind}]{extra} — every {self.cfg.inbox_poll_minutes} min")
        if not self.inboxes:
            lines.append("❌ No inboxes configured")
        c = self.contacts()
        lines.append(f"👥 {len(c.groups)} groups, {len(c.people)} contacts")
        lines.append(f"🕗 Daily digest: {self.cfg.digest_time or 'off'} ({self.cfg.timezone})")
        await self.reply(update, "\n".join(lines))

    async def list_contacts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        c = self.contacts()
        if not c.groups and not c.people:
            await self.reply(update, f"No contacts yet. Create <code>{esc(str(self.cfg.contacts_file))}</code> (see contacts.example.yaml).")
            return
        lines = ["<b>Groups</b>"]
        for name, g in c.groups.items():
            r = resolve_recipients(name, c)
            lines.append(f"• <code>{esc(name)}</code> — {len(r.emails)} emails, {len(r.phones)} WhatsApp")
        lines.append("\n<b>Contacts</b>")
        for name, p in c.people.items():
            lines.append(f"• <code>{esc(name)}</code> {esc(p['email'])} {esc(p['whatsapp'])}")
        await self.reply(update, "\n".join(lines))

    # ------------------------------------------------------------------ announcements
    async def _prepare_send(self, update: Update, args: str, kind: str) -> None:
        if kind == "whatsapp":
            parts = split_args(args, 2)
            if len(parts) < 2 or not parts[0] or not parts[1]:
                await self.reply(update, "Usage: <code>/whatsapp to | message</code>")
                return
            to, body, subject = parts[0], parts[1], ""
        else:
            parts = split_args(args, 3)
            if len(parts) < 3 or not all(parts):
                await self.reply(update, f"Usage: <code>/{kind} to | subject | message</code>")
                return
            to, subject, body = parts

        rec = resolve_recipients(to, self.contacts())
        want_email = kind in ("email", "announce")
        want_wa = kind in ("whatsapp", "announce")
        emails = rec.emails if want_email else []
        phones = rec.phones if want_wa else []

        problems = []
        if rec.unknown:
            problems.append(f"⚠️ Unknown names (skipped): {esc(', '.join(rec.unknown))}")
        if want_email and not self.cfg.smtp.enabled:
            problems.append("⚠️ Email is not configured — emails will be skipped.")
            emails = []
        if want_wa and not self.cfg.whatsapp.enabled:
            problems.append("⚠️ WhatsApp is not configured — WhatsApp will be skipped.")
            phones = []
        if not emails and not phones:
            await self.reply(update, "Nobody to send to.\n" + "\n".join(problems))
            return

        pid = uuid.uuid4().hex[:10]
        self.pending[pid] = {"emails": emails, "phones": phones, "subject": subject, "body": body}
        preview = ["<b>Ready to send — please confirm</b>"]
        if emails:
            preview.append(f"📧 Email to {len(emails)}: {esc(', '.join(emails))}")
            preview.append(f"<b>Subject:</b> {esc(subject)}")
        if phones:
            preview.append(f"💬 WhatsApp to {len(phones)}: {esc(', '.join('+' + p for p in phones))}")
        preview.append(f"\n{esc(body)}")
        preview.extend(problems)
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send", callback_data=f"send:{pid}"),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{pid}"),
        ]])
        await self.reply(update, "\n".join(preview), markup)

    async def cmd_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._prepare_send(update, _raw_args(update), "email")

    async def cmd_whatsapp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._prepare_send(update, _raw_args(update), "whatsapp")

    async def cmd_announce(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._prepare_send(update, _raw_args(update), "announce")

    async def on_send_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        action, pid = q.data.split(":", 1)
        job = self.pending.pop(pid, None)
        if job is None:
            await q.edit_message_reply_markup(None)
            await q.message.reply_text("That request expired (the bot may have restarted). Please send the command again.")
            return
        if action == "cancel":
            await q.edit_message_reply_markup(None)
            await q.message.reply_text("Cancelled. Nothing was sent.")
            return

        await q.edit_message_reply_markup(None)
        lines = ["<b>Results</b>"]
        if job["emails"]:
            try:
                res = await self.mailer.send(job["emails"], job["subject"], job["body"])
                ok = [k for k, v in res.items() if v == "ok"]
                lines.append(f"📧 Email: {len(ok)}/{len(res)} sent")
                lines += [f"   ❌ {esc(k)}: {esc(v)}" for k, v in res.items() if v != "ok"]
            except Exception as exc:
                log.exception("email send failed")
                lines.append(f"📧 Email failed: {esc(str(exc))}")
        if job["phones"]:
            try:
                res = await self.wa.send(job["phones"], job["body"])
                ok = [k for k, v in res.items() if v == "ok"]
                lines.append(f"💬 WhatsApp: {len(ok)}/{len(res)} sent")
                lines += [f"   ❌ +{esc(k)}: {esc(v)}" for k, v in res.items() if v != "ok"]
            except Exception as exc:
                log.exception("whatsapp send failed")
                lines.append(f"💬 WhatsApp failed: {esc(str(exc))}")
        await self.reply(update, "\n".join(lines))

    # ------------------------------------------------------------------ calendar
    async def cmd_event(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        parts = split_args(_raw_args(update), 4)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await self.reply(update, "Usage: <code>/event title | when | duration | location</code>\n"
                                     "e.g. <code>/event Dentist | next tuesday 4pm | 30m</code>")
            return
        title, when = parts[0], parts[1]
        try:
            start = parse_when(when, self.cfg.timezone)
            duration = parse_duration(parts[2] if len(parts) > 2 else None)
        except ValueError as exc:
            await self.reply(update, esc(str(exc)))
            return
        location = parts[3] if len(parts) > 3 else None
        try:
            ev = await self.cal.add_event(title, start, duration, location=location)
        except Exception as exc:
            log.exception("calendar insert failed")
            await self.reply(update, f"Could not add the event: {esc(str(exc))}")
            return
        when_txt = start.strftime("%a %d %b %Y") + (" (all day)" if duration is None else
                                                     f", {start:%H:%M}–{(start + duration):%H:%M}")
        link = ev.get("htmlLink")
        await self.reply(update, f"📅 Added <b>{esc(title)}</b>\n{esc(when_txt)}"
                         + (f"\n📍 {esc(location)}" if location else "")
                         + (f"\n<a href=\"{esc(link)}\">Open in Google Calendar</a>" if link else ""))

    async def _agenda_text(self, days: int) -> str:
        now = datetime.now(self.tz)
        events = await self.cal.list_events(now, now + timedelta(days=days))
        if not events:
            return f"No events in the next {days} days."
        lines = []
        for e in events:
            start = e.get("start", {})
            if "dateTime" in start:
                label = self.fmt_dt(datetime.fromisoformat(start["dateTime"]))
            else:
                label = datetime.fromisoformat(start["date"]).strftime("%a %d %b") + ", all day"
            lines.append(f"• {esc(label)} — {esc(e.get('summary', '(no title)'))}")
        return "\n".join(lines)

    async def cmd_agenda(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = _int_arg(context, 7)
        try:
            text = await self._agenda_text(days)
        except Exception as exc:
            await self.reply(update, f"Calendar error: {esc(str(exc))}")
            return
        await self.reply(update, f"<b>📅 Next {days} days</b>\n{text}")

    # ------------------------------------------------------------------ canvas
    def _assignment_line(self, a) -> str:
        return (f"• <b>{esc(a.course)}</b>: <a href=\"{esc(a.url)}\">{esc(a.name)}</a>\n"
                f"   due {esc(self.fmt_due(a.due_at))}")

    def _calendar_button(self, a) -> InlineKeyboardMarkup | None:
        if not self.cfg.calendar.enabled or a.due_at is None:
            return None
        self.store.set(f"asg:{a.id}", {
            "title": f"{a.course}: {a.name}", "due": a.due_at.isoformat(), "url": a.url,
        })
        return InlineKeyboardMarkup([[InlineKeyboardButton("📅 Add deadline to calendar", callback_data=f"cal:{a.id}")]])

    async def cmd_assignments(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = _int_arg(context, 14)
        try:
            items = await self.canvas.upcoming_assignments(days=days)
        except Exception as exc:
            await self.reply(update, f"Canvas error: {esc(str(exc))}")
            return
        if not items:
            await self.reply(update, f"🎉 Nothing unsubmitted due in the next {days} days.")
            return
        await self.reply(update, f"<b>🎓 Due in the next {days} days</b>\n" + "\n".join(self._assignment_line(a) for a in items))

    async def on_calendar_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        info = self.store.get(f"asg:{q.data.split(':', 1)[1]}")
        if not info:
            await q.message.reply_text("I don't have that assignment's details anymore.")
            return
        due = datetime.fromisoformat(info["due"]).astimezone(self.tz)
        try:
            # 30-minute block ending at the deadline, with reminders 1 day and 2 hours before.
            await self.cal.add_event(f"⏰ {info['title']}", due - timedelta(minutes=30), timedelta(minutes=30),
                                     description=info["url"], reminder_minutes=[24 * 60, 120])
        except Exception as exc:
            await q.message.reply_text(f"Could not add it: {exc}")
            return
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"📅 Added deadline: {info['title']} ({self.fmt_dt(due)})")

    async def check_canvas(self, app: Application) -> int:
        """Report new announcements, new assignments and assignments due within 24h. Returns count reported."""
        if not self.cfg.canvas.enabled:
            return 0
        sent = 0
        announcements = await self.canvas.recent_announcements(days=7)
        assignments = await self.canvas.upcoming_assignments(days=30)

        first_ann = not self.store.is_baselined("canvas_ann")
        for a in announcements:
            if self.store.is_seen("canvas_ann", a.id):
                continue
            self.store.mark_seen("canvas_ann", a.id)
            if first_ann:
                continue
            body = truncate(html_to_text(a.message_html), 1200)
            await self.notify(app, f"📢 <b>Canvas announcement — {esc(a.course)}</b>\n"
                                   f"<a href=\"{esc(a.url)}\">{esc(a.title)}</a>\n"
                                   f"<i>{esc(a.author)} · {esc(self.fmt_dt(a.posted_at))}</i>\n\n{esc(body)}")
            sent += 1
        self.store.set_baselined("canvas_ann")

        first_asg = not self.store.is_baselined("canvas_asg")
        now = datetime.now(self.tz)
        for a in assignments:
            if not self.store.is_seen("canvas_asg", a.id):
                self.store.mark_seen("canvas_asg", a.id)
                if not first_asg:
                    await self.notify(app, "🆕 <b>New assignment</b>\n" + self._assignment_line(a), self._calendar_button(a))
                    sent += 1
            if a.due_at and a.due_at - now <= timedelta(hours=24) and not self.store.is_seen("canvas_due24", a.id):
                self.store.mark_seen("canvas_due24", a.id)
                await self.notify(app, "⏰ <b>Due within 24 hours</b>\n" + self._assignment_line(a))
                sent += 1
        self.store.set_baselined("canvas_asg")
        return sent

    async def cmd_canvas(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, "Checking Canvas…")
        try:
            n = await self.check_canvas(context.application)
        except Exception as exc:
            await self.reply(update, f"Canvas error: {esc(str(exc))}")
            return
        if n == 0:
            await self.reply(update, "Nothing new on Canvas. (Use /assignments to see everything due.)")

    # ------------------------------------------------------------------ inboxes
    async def check_inboxes(self, app: Application) -> tuple[int, list[str]]:
        sent, errors = 0, []
        for ic, inbox in self.inboxes:
            kind = f"mail:{ic.key}"
            try:
                mails = await inbox.fetch_recent(since_days=2)
            except Exception as exc:
                log.warning("inbox %s failed: %s", ic.name, exc)
                errors.append(f"{ic.name}: {exc}")
                continue
            first = not self.store.is_baselined(kind)
            for m in mails:
                if self.store.is_seen(kind, m.uid):
                    continue
                self.store.mark_seen(kind, m.uid)
                if first or not matches_filters(m.sender, m.subject, m.snippet, ic.filter_from, ic.filter_keywords):
                    continue
                await self.notify(app, f"📧 <b>{esc(ic.name)}</b>\n<b>From:</b> {esc(m.sender)}\n"
                                       f"<b>Subject:</b> {esc(m.subject)}\n<i>{esc(self.fmt_dt(m.received))}</i>\n\n"
                                       f"{esc(truncate(m.snippet, 600))}")
                sent += 1
            self.store.set_baselined(kind)
        return sent, errors

    async def cmd_inbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.inboxes:
            await self.reply(update, "No inboxes configured (INBOX1_*, INBOX2_* in .env).")
            return
        await self.reply(update, "Checking inboxes…")
        n, errors = await self.check_inboxes(context.application)
        msg = [] if n else ["No new matching emails."]
        msg += [f"⚠️ {esc(e)}" for e in errors]
        if msg:
            await self.reply(update, "\n".join(msg))

    async def cmd_outlook_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        graph = [(ic, ib) for ic, ib in self.inboxes if isinstance(ib, GraphInbox)]
        if not graph:
            await self.reply(update, "No Outlook/Microsoft 365 inbox is configured (INBOXn_TYPE=graph).")
            return
        for ic, inbox in graph:
            try:
                flow = inbox.start_device_login()
            except Exception as exc:
                await self.reply(update, f"{esc(ic.name)}: {esc(str(exc))}")
                continue
            await self.reply(update, f"🔑 <b>{esc(ic.name)}</b>\nOpen {esc(flow['verification_uri'])} and enter code "
                                     f"<code>{esc(flow['user_code'])}</code>\nSign in with that email account. I'll wait.")
            result = await inbox.finish_device_login(flow)
            if "access_token" in result:
                await self.reply(update, f"✅ {esc(ic.name)} signed in. I'll check it every {self.cfg.inbox_poll_minutes} min.")
            else:
                await self.reply(update, f"❌ {esc(ic.name)} sign-in failed: {esc(result.get('error_description', str(result)))}")

    # ------------------------------------------------------------------ scheduled jobs
    async def job_canvas(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.check_canvas(context.application)
        except Exception:
            log.exception("scheduled canvas check failed")

    async def job_inbox(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        _, errors = await self.check_inboxes(context.application)
        # Only bother you about a broken inbox once a day, not every poll.
        for err in errors:
            key = f"inbox_err:{datetime.now(self.tz).date()}:{err[:80]}"
            if not self.store.get(key):
                self.store.set(key, True)
                await self.notify(context.application, f"⚠️ Inbox problem: {esc(err)}")

    async def job_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        parts = [f"☀️ <b>Good morning! {datetime.now(self.tz):%A %d %B}</b>"]
        if self.cfg.calendar.enabled:
            try:
                parts.append("\n<b>📅 Today & tomorrow</b>\n" + await self._agenda_text(2))
            except Exception as exc:
                parts.append(f"\nCalendar error: {esc(str(exc))}")
        if self.cfg.canvas.enabled:
            try:
                items = await self.canvas.upcoming_assignments(days=3)
                parts.append("\n<b>🎓 Due in the next 3 days</b>\n"
                             + ("\n".join(self._assignment_line(a) for a in items) if items else "Nothing 🎉"))
            except Exception as exc:
                parts.append(f"\nCanvas error: {esc(str(exc))}")
        await self.notify(context.application, "\n".join(parts))


def _raw_args(update: Update) -> str:
    """Everything after the /command, keeping newlines (context.args would drop them)."""
    text = update.effective_message.text or ""
    return text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""


def _int_arg(context: ContextTypes.DEFAULT_TYPE, default: int) -> int:
    try:
        return max(1, min(90, int(context.args[0]))) if context.args else default
    except ValueError:
        return default


def owner_only(cfg: Config, handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id not in cfg.owner_ids:
            log.warning("blocked user %s", user.id if user else None)
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"This is a private bot. Your Telegram user ID is {user.id if user else '?'}."
                    " (If this is your bot, put it in TELEGRAM_OWNER_IDS in .env and restart.)"
                )
            elif update.callback_query:
                await update.callback_query.answer("Not allowed", show_alert=True)
            return
        return await handler(update, context)
    return wrapper


def build_app(cfg: Config) -> Application:
    b = Bot(cfg)
    app = ApplicationBuilder().token(cfg.telegram_token).build()
    guard = functools.partial(owner_only, cfg)

    commands = {
        "start": b.start, "help": b.start, "status": b.status, "contacts": b.list_contacts,
        "email": b.cmd_email, "whatsapp": b.cmd_whatsapp, "announce": b.cmd_announce,
        "event": b.cmd_event, "agenda": b.cmd_agenda,
        "assignments": b.cmd_assignments, "canvas": b.cmd_canvas,
        "inbox": b.cmd_inbox, "outlook_login": b.cmd_outlook_login,
    }
    for name, fn in commands.items():
        # outlook_login waits for you to finish signing in, so don't block other commands meanwhile
        app.add_handler(CommandHandler(name, guard(fn), block=name != "outlook_login"))
    app.add_handler(CallbackQueryHandler(guard(b.on_send_button), pattern=r"^(send|cancel):"))
    app.add_handler(CallbackQueryHandler(guard(b.on_calendar_button), pattern=r"^cal:"))

    jq = app.job_queue
    if cfg.canvas.enabled:
        jq.run_repeating(b.job_canvas, interval=cfg.canvas.poll_minutes * 60, first=20, name="canvas")
    if b.inboxes:
        jq.run_repeating(b.job_inbox, interval=cfg.inbox_poll_minutes * 60, first=40, name="inbox")
    if cfg.digest_time and cfg.digest_time.lower() != "off":
        hh, mm = (int(x) for x in cfg.digest_time.split(":"))
        jq.run_daily(b.job_digest, time=time(hh, mm, tzinfo=ZoneInfo(cfg.timezone)), name="digest")

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("unhandled error", exc_info=context.error)

    app.add_error_handler(on_error)
    return app


def main() -> None:
    cfg = load_config()
    if not cfg.owner_ids:
        log.warning("TELEGRAM_OWNER_IDS is empty: the bot will only tell people their user ID. "
                    "Message it, copy your ID into .env, and restart.")
    app = build_app(cfg)
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
