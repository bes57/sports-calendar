# K-Cal

A personal sports planning tool: Google Calendar–style view of upcoming events
across the leagues I follow, plus a daily morning digest by email (and
optionally SMS).

See [GOAL.md](./GOAL.md) for the full goal/scope.

## What's in v1

- **9 leagues live**: MLB, NBA, WNBA, NHL, UFC, MMA (PFL), F1, IPL, Valorant
- **Calendar UI**: month / week / day / list views, color-coded per league,
  click-through popovers with venue, broadcast, and source link
- **League filter**: toggle any league on/off; choice persists in the browser
- **Daily digest**: HTML email grouped by league with start times in your
  timezone, broadcast info, and links back to ESPN / vlr.gg
- **Auto-refresh**: background scheduler re-fetches every 30 min (configurable)
- **Self-contained**: SQLite for storage, no external DB needed

## Quick start

```bash
cd sports-calendar
./run.sh
```

That will:
1. Create a Python venv (`.venv/`) if needed
2. Install dependencies
3. Copy `.env.example` → `.env` if missing
4. Start the web server at <http://127.0.0.1:8765>

First time, click **Refresh data** in the sidebar to populate events.
(After that, the background scheduler keeps things fresh.)

## Configuration

Edit `.env` to enable the digest and tune behavior. The most important keys:

| Key | What it does |
|---|---|
| `TZ` | Your local timezone (e.g. `America/New_York`) |
| `DIGEST_TIME` | Time of day to send the digest (24h, e.g. `07:00`) |
| `DIGEST_EMAIL_TO` | Where to send the digest |
| `RESEND_API_KEY` + `RESEND_FROM` | Use [Resend](https://resend.com) for email (recommended; free tier covers personal use) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | Or use plain SMTP (e.g. Gmail App Password) |
| `TWILIO_*` + `DIGEST_SMS_TO` | Optional: SMS via Twilio |
| `FETCH_DAYS_AHEAD` | How far ahead to pull events (default 30) |
| `REFRESH_INTERVAL_MIN` | How often to auto-refresh (default 30 min) |

If no email provider is configured, `POST /api/digest/send` writes the rendered
digest to `data/last_digest.html` so you can still preview it.

## Data sources

| League | Source | Endpoint |
|---|---|---|
| MLB, NBA, WNBA, NHL, UFC, MMA (PFL), IPL | ESPN scoreboard | `site.api.espn.com/apis/site/v2/sports/...` |
| F1 | ESPN scoreboard, split into per-session events (FP1/FP2/FP3/Quali/Race) | same as above |
| Valorant | [vlrggapi](https://vlrggapi.vercel.app) (community vlr.gg scraper) | `vlrggapi.vercel.app/match?q=upcoming` |

All sources are public, no API keys required.

## URLs

| Path | What |
|---|---|
| `/` | Calendar |
| `/settings` | Per-league status, digest preview, manual refresh |
| `GET /api/events?start=&end=&leagues=mlb,nba` | Events JSON (FullCalendar-shaped) |
| `GET /api/leagues` | League registry |
| `POST /api/refresh` | Background refresh (all or `?league=mlb`) |
| `POST /api/refresh/sync` | Refresh and wait for the result |
| `GET /api/refresh/status` | When each league was last refreshed and whether it succeeded |
| `GET /api/digest/preview?format=html\|text` | Today's digest, rendered |
| `POST /api/digest/send` | Send today's digest now |

## Adding a new league

1. Open `leagues.py` and add a new `League(...)` entry. Pick a unique `id`, a
   readable `name`, a `color`, and point `source` at one of the registered
   fetchers in `sources/__init__.py`.
2. If your league uses an existing source (most ESPN sports), just set
   `source_args={"sport": "...", "league": "..."}` and you're done.
3. If it needs a brand-new fetcher, add a `sources/<thing>.py` module that
   exposes `fetch_<thing>(source_args, days_ahead) -> list[Event]`, then
   register it in `sources/__init__.py`.

## File layout

```
sports-calendar/
├── GOAL.md             goal/scope document
├── README.md           this file
├── requirements.txt
├── .env.example        copy to .env to configure email/SMS/timezone
├── run.sh              one-command launcher
├── app.py              FastAPI app — routes, startup
├── leagues.py          league registry (id, color, source)
├── db.py               SQLite schema + helpers
├── refresh.py          fetches all leagues and upserts to DB
├── digest.py           digest formatter + email/SMS senders
├── scheduler.py        APScheduler — periodic refresh + daily digest
├── sources/
│   ├── __init__.py     dispatcher: source name -> fetcher
│   ├── espn.py         generic ESPN parser (+ F1 session splitter)
│   └── valorant.py     vlrggapi parser
├── templates/
│   ├── base.html
│   ├── calendar.html
│   └── settings.html
├── static/
│   ├── app.css
│   └── app.js
└── data/
    └── events.db       SQLite (created on first run)
```

## Deploying to Railway (or Fly / Render)

K-Cal ships a `Procfile` (`web: uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}`)
and a `.python-version` (`3.13`) so PaaS platforms can detect it automatically.

### Railway

1. **New Project → Deploy from GitHub repo** → pick `sports-calendar`.
2. After the first deploy, set these env vars under **Variables**:
   - `TZ` = `America/New_York`
   - `DAILY_DIGEST_ENABLED` = `false` — leave the daily ntfy push to the
     GitHub Actions cron so you don't get double-pushed. Set to `true` (or
     omit) if you'd rather have Railway send the push instead.
   - `NTFY_TOPIC` = your topic (only needed if you set `DAILY_DIGEST_ENABLED=true`)
3. Railway's filesystem is ephemeral. The first request after a deploy
   triggers a background ESPN refresh (see `on_startup` in `app.py`) so the
   calendar populates within ~30s. If you want persistence across deploys,
   add a Railway volume mounted at `/app/data`.

### Generic

Any container platform that runs `uvicorn` works:

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

## Deploying

The app is a standard ASGI server — run it anywhere that runs Python:

- **Locally**: `./run.sh` (what you'll do most of the time).
- **Always-on box**: a $5/mo VPS or a free Fly.io / Render web service. Use
  `gunicorn -k uvicorn.workers.UvicornWorker app:app` for production.
- **macOS background**: wrap `run.sh` in a `launchd` plist so it starts at
  login. The internal scheduler then handles refresh + digest.

The digest scheduler runs inside the same process as the web app, so the
process needs to stay up for the daily email to fire.

## Things to consider for v2

- Favorite-team filter (highlight your teams in the calendar / digest)
- More leagues — NFL, College FB/BB, Premier League, EPL, NCAA, esports
  beyond Valorant
- iCal feed export (`.ics`) so you can subscribe to it from native Calendar
- Multi-user with auth (currently single-user / single-recipient)
