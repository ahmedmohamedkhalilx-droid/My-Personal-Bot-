# My Personal Bot

A private Telegram bot that runs 24/7 on a server and does three jobs:

1. **Announcements:** sends email and WhatsApp messages when you give a command (`/email`, `/whatsapp`, `/announce`). Before anything goes out it shows you a preview with **Send / Cancel** buttons.
2. **Calendar:** adds events to Google Calendar from plain language (`/event Study group | tomorrow 6pm | 2h`) and shows what's coming up (`/agenda`).
3. **Canvas and email watcher:**
   - checks Canvas for new assignments and announcements
   - warns you when an assignment is due within 24 hours
   - watches your two email inboxes
   - sends a digest every morning

   New assignments come with a "📅 Add deadline to calendar" button.

Only the Telegram user IDs listed in `TELEGRAM_OWNER_IDS` can use the bot.

## Commands

| Command | Example |
|---|---|
| `/email to \| subject \| message` | `/email team \| Meeting moved \| We meet at 5pm in room 201` |
| `/whatsapp to \| message` | `/whatsapp family, +201001234567 \| Landing at 9pm` |
| `/announce to \| subject \| message` | Sends both email and WhatsApp |
| `/contacts` | Lists your groups and contacts |
| `/event title \| when \| duration \| location` | `/event Dentist \| next tuesday 4pm \| 30m` (duration: `45m`, `1h30m`, `all day`, default 1h) |
| `/agenda [days]` | `/agenda 3` |
| `/assignments [days]` | Unsubmitted Canvas work due soon (default 14 days) |
| `/canvas` | Check Canvas now |
| `/inbox` | Check both inboxes now |
| `/outlook_login` | One-time sign-in for an Outlook / Microsoft 365 inbox |
| `/status` | Shows what's configured |

`to` can be a mix of group names, contact names (both from `contacts.yaml`), email addresses and phone numbers, separated by commas.

The first time the bot checks Canvas or an inbox, it only records what is already there. After that it tells you about new items, so you don't get flooded with old ones.

## Setup

### 1. Telegram bot
1. Message [@BotFather](https://t.me/BotFather) and send `/newbot`. Copy the token into `TELEGRAM_BOT_TOKEN`.
2. Start the bot once with `TELEGRAM_OWNER_IDS` empty and send it any message. It replies with your user ID.
3. Put that ID in `TELEGRAM_OWNER_IDS` and restart the bot.

### 2. Sending email (SMTP)
With Gmail:
1. Turn on 2-Step Verification for your Google account.
2. Go to Google Account → Security → **App passwords** and create one.
3. Set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER` to your address, and `SMTP_PASSWORD` to the 16-character app password.

### 3. WhatsApp (Meta WhatsApp Cloud API)
1. At [developers.facebook.com](https://developers.facebook.com/), create an app and add the **WhatsApp** product.
2. Add and verify a phone number. Use a number that isn't already on your personal WhatsApp.
3. Copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`.
4. Create a permanent token: Business Settings → System users → generate a token with `whatsapp_business_messaging`. Put it in `WHATSAPP_TOKEN`.
5. **Important:** WhatsApp only lets a business number send free text to people who messaged it in the last 24 hours. To reach everyone else, create a message template in WhatsApp Manager whose body is just `{{1}}` (category "Utility"). Once Meta approves it, put its name in `WHATSAPP_TEMPLATE_NAME` and the bot will send your announcement through it.
6. The official API **cannot post into WhatsApp groups**. It sends to each person individually.

### 4. Google Calendar
1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Google Calendar API**.
2. Go to IAM → Service Accounts → create one → Keys → Add key → JSON. Save the file as `secrets/google-service-account.json`.
3. In Google Calendar, open your calendar's **Settings and sharing** → **Share with specific people**. Add the service account's email with **Make changes to events**.
4. Set `GOOGLE_CALENDAR_ID` to the calendar ID, which is usually your Gmail address.

### 5. Canvas
1. In Canvas, go to Account → Settings → **+ New Access Token**. Put the token in `CANVAS_TOKEN`.
2. Set `CANVAS_BASE_URL` to the address you use to open Canvas, for example `https://aucegypt.instructure.com`.

### 6. The two inboxes
- **Gmail:** set `INBOXn_TYPE=imap` and `INBOXn_HOST=imap.gmail.com`, with your address and an app password (the same kind as in step 2).
- **University Outlook / Microsoft 365:** Microsoft has turned off password logins for IMAP, so this inbox uses Microsoft Graph instead:
  1. In [Azure portal](https://portal.azure.com/), go to **App registrations** → New registration. Choose "Accounts in any organizational directory and personal Microsoft accounts".
  2. Under **Authentication**, set "Allow public client flows" to **Yes**.
  3. Under **API permissions**, add Microsoft Graph → Delegated → `Mail.Read`.
  4. Copy the **Application (client) ID** into `INBOX2_CLIENT_ID`. Then send `/outlook_login` to the bot and follow the link. You only do this once; the token is saved in `data/` and refreshed automatically.

  If your university blocks third-party apps (you'll see "needs admin approval"), there's a simple workaround: set up **auto-forwarding** from the university mailbox to Gmail and watch that inbox over IMAP instead.

Each inbox can use filters:
- `INBOXn_FILTER_FROM`: sender addresses or domains, e.g. `aucegypt.edu`
- `INBOXn_FILTER_KEYWORDS`: words to look for in the subject or body, e.g. `announcement,deadline`

The bot only reports emails that match at least one filter. If you leave both empty, it reports everything.

## Running it 24/7

You need a small Linux server, for example a $4–6/month VPS from Hetzner, DigitalOcean or Lightsail, or the Oracle Cloud free tier. The bot uses polling, so you don't need a domain, HTTPS or open ports.

### Option A: Docker (recommended)
```bash
git clone <this repo> && cd My-Personal-Bot-
cp .env.example .env              # fill it in
cp contacts.example.yaml contacts.yaml
mkdir -p data secrets             # put google-service-account.json in secrets/
docker compose up -d --build
docker compose logs -f            # watch it start
```
`restart: unless-stopped` brings the bot back after a crash or a server reboot.

### Option B: systemd, without Docker
```bash
sudo useradd -r -m bot
sudo git clone <this repo> /opt/My-Personal-Bot- && cd /opt/My-Personal-Bot-
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo cp contacts.example.yaml contacts.yaml   # fill them in
sudo chown -R bot:bot /opt/My-Personal-Bot-
sudo cp deploy/personal-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now personal-bot
journalctl -u personal-bot -f
```

### Running locally for testing
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m bot
```

## Development
```bash
pip install pytest && python -m pytest
```

Project layout:
- `bot/main.py`: commands, confirm buttons and scheduled jobs
- `bot/services/`: one file per integration (email, WhatsApp, calendar, Canvas, inboxes)
- `bot/utils.py`: parsing helpers
- `bot/storage.py`: SQLite file that remembers what you've already been told about

**Keep `.env`, `contacts.yaml`, `secrets/` and `data/` private.** They're already in `.gitignore`.
