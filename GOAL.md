# K-Cal — Project Goal

## One-line pitch
A personal sports planning tool: a Google Calendar–style web view of upcoming events across the leagues I follow, plus a daily morning digest (email and/or SMS) so I always know what's on.

## Why
I want a single, customizable place to see what sports are happening today, this week, and beyond — without juggling ESPN tabs, league apps, and broadcast schedules. The daily digest means I don't even have to open the site to know what's coming.

## Users
Just me, at least at the start. Designed for personal use; multi-user is a nice-to-have, not a v1 goal.

## Leagues / sports in scope (initial)
- **MLB** — Major League Baseball
- **IPL** — Indian Premier League (cricket)
- **WNBA** — Women's National Basketball Association
- **NBA** — National Basketball Association
- **UFC** — Ultimate Fighting Championship
- **NHL** — National Hockey League
- **Valorant** — pro esports circuit (VCT)
- **F1** — Formula 1
- **MMA** — broader MMA promotions beyond UFC (PFL, Bellator, ONE, etc.)

More leagues to be added incrementally. The system should make adding a new league a small, contained change.

## Core features

### Web app
- **Google Calendar–style view** — month, week, and day views
- **Color-coded by league** — each league gets a distinct color, consistent across views and the digest
- **Filterable** — toggle leagues on/off; filter state persists
- **Event detail** — teams/competitors, start time in my timezone, broadcast/streaming info, venue, round/series context (e.g. "Game 6, Eastern Conf Finals")
- **Auto-updating** — calendar reflects schedule changes (postponements, time shifts) without manual refresh

### Daily digest
- Delivered each morning at a configurable time (default ~7am local)
- Email and/or SMS
- Grouped by league, in time order, includes broadcast info
- Only includes leagues I've opted into

### Customization
- Pick which leagues appear
- Pick favorite teams/competitors (optional; used for highlighting or filtering "must-watch")
- Digest delivery time and timezone
- Channel choice: email, SMS, or both

## Non-goals (for v1)
- Live scores / in-game updates
- Betting odds, market data, or any trading-related features
- Historical stats or analytics
- Mobile native app (a responsive web app / PWA is enough)
- Social / sharing features

## Success looks like
- I open the site once and instantly see what's on this week
- The morning email tells me everything worth planning around, in under 30 seconds of reading
- Adding a new league takes me an hour or two, not a weekend
- It runs itself — I don't have to babysit data feeds

## Tech direction (current thinking, not locked in)
- **Frontend / hosting**: Next.js on Vercel
- **Database / auth**: Supabase (Postgres)
- **Calendar UI**: FullCalendar.js or react-big-calendar
- **Data sources**: ESPN's free JSON endpoints as the primary feed; TheSportsDB as a fallback; per-league specialty sources where needed (e.g. VLR.gg for Valorant, formula1.com for F1)
- **Scheduled jobs**: Vercel Cron — frequent refresh for schedules, daily run for the digest
- **Email**: Resend
- **SMS**: Twilio (optional, opt-in per channel)
- **Cost target**: under $5/month at personal-use scale

## Open questions to resolve before building
- Which leagues' schedules are not covered by ESPN's free endpoints, and what's the fallback for each? (Valorant and non-UFC MMA are the obvious gaps)
- One unified `events` table, or one table per sport? (Leaning unified with a flexible `metadata` JSON column)
- How far ahead should the calendar show events — 30 days, full season, indefinite?
- Do I want a "must-watch" tier separate from the all-events view?
