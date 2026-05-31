"""Daily digest builder + sender.

Builds a plain-text and HTML version of "today's sports" grouped by league, in
the user's local timezone. Sends via Resend (preferred) or SMTP. Optionally
sends a shortened SMS via Twilio.
"""

from __future__ import annotations

import io
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

import httpx
import pytz
from PIL import Image, ImageDraw, ImageFont

from db import get_events
from leagues import LEAGUES, by_id


def _local_tz() -> pytz.BaseTzInfo:
    return pytz.timezone(os.getenv("TZ", "America/New_York"))


def _day_window_utc(day_offset: int = 0) -> tuple[str, str, datetime]:
    """Return (start_iso_utc, end_iso_utc, local_date) for today + offset in local TZ."""
    tz = _local_tz()
    now_local = datetime.now(tz)
    day = (now_local + timedelta(days=day_offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_local = day
    end_local = day + timedelta(days=1)
    return (
        start_local.astimezone(pytz.UTC).isoformat(),
        end_local.astimezone(pytz.UTC).isoformat(),
        day,
    )


def _sports_day_window_utc(day_offset: int = 0) -> tuple[str, str, datetime]:
    """Window from `day` 7am local through the next day 5am local.

    Used by the SMS digest so it captures the natural 'sports day' span — from
    when morning broadcasts pick up through the tail end of overnight games on
    the West Coast / Asia. Caller can shift the day with `day_offset`.
    """
    tz = _local_tz()
    now_local = datetime.now(tz)
    start_local = (now_local + timedelta(days=day_offset)).replace(
        hour=7, minute=0, second=0, microsecond=0
    )
    end_local = (now_local + timedelta(days=day_offset + 1)).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    return (
        start_local.astimezone(pytz.UTC).isoformat(),
        end_local.astimezone(pytz.UTC).isoformat(),
        start_local,
    )


def _format_time(iso: str) -> str:
    tz = _local_tz()
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%-I:%M %p %Z")
    except Exception:
        return iso


_EMOJI_PALETTE = [
    # (r,    g,    b,    emoji)
    (220,   38,   38, "🟥"),  # red
    (249,  115,   22, "🟧"),  # orange
    (234,  179,    8, "🟨"),  # yellow
    ( 20,  130,   50, "🟩"),  # green — darker anchor so deep-greens (golf
                              # fairway, IPL) don't fall to ⬛
    ( 59,  130,  246, "🟦"),  # blue
    (124,   58,  237, "🟪"),  # purple — using the Valorant shade so
                              # indigos/violets map cleanly
    (146,   64,   14, "🟫"),  # brown
    ( 30,   41,   59, "⬛"),  # black/slate
]


def _color_emoji(hex_color: str | None) -> str:
    """Closest emoji color square for a #RRGGBB hex. Used to give each league
    a visible color marker inside plain-text ntfy notifications."""
    if not hex_color or not hex_color.startswith("#"):
        return "⬜"
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return "⬜"
    return min(
        _EMOJI_PALETTE,
        key=lambda p: (p[0] - r) ** 2 + (p[1] - g) ** 2 + (p[2] - b) ** 2,
    )[3]


def _format_time_compact(iso: str) -> str:
    """Tight format for SMS: '1:35p', '12:00a'. Drops minutes-zero? No, keep
    so 'top of the hour' games still sort visually with others."""
    tz = _local_tz()
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        ampm = "a" if dt.strftime("%p") == "AM" else "p"
        return dt.strftime("%-I:%M") + ampm
    except Exception:
        return iso


def _group_by_league(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in events:
        out.setdefault(e["league"], []).append(e)
    return out


def build_digest_text(day_offset: int = 0) -> str:
    start, end, day = _day_window_utc(day_offset)
    events = get_events(start_iso=start, end_iso=end)
    if not events:
        return f"{day.strftime('%A, %B %-d')}\n\nNo events today.\n"

    grouped = _group_by_league(events)
    lines = [day.strftime("%A, %B %-d, %Y"), ""]
    for lg in LEAGUES:  # iterate in canonical order so output is stable
        if lg.id not in grouped:
            continue
        es = grouped[lg.id]
        lines.append(f"{lg.name} — {len(es)} event{'s' if len(es) != 1 else ''}")
        for e in es:
            t = _format_time(e["start_utc"])
            line = f"  {t}  {e['title']}"
            extras = []
            if e.get("broadcast"):
                extras.append(e["broadcast"])
            if e.get("subtitle") and e["subtitle"] not in e["title"]:
                extras.append(e["subtitle"])
            if extras:
                line += "  (" + " — ".join(extras) + ")"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_digest_html(day_offset: int = 0) -> str:
    start, end, day = _day_window_utc(day_offset)
    events = get_events(start_iso=start, end_iso=end)
    title = day.strftime("%A, %B %-d, %Y")
    if not events:
        return f"""<html><body style="font-family: -apple-system, system-ui, sans-serif;">
<h2>{title}</h2><p>No events today.</p></body></html>"""

    grouped = _group_by_league(events)
    parts = [
        '<html><body style="font-family: -apple-system, system-ui, sans-serif; '
        'max-width: 640px; margin: 0 auto; color: #111827;">',
        f"<h2 style='margin: 0 0 16px;'>{title}</h2>",
    ]
    for lg in LEAGUES:
        if lg.id not in grouped:
            continue
        es = grouped[lg.id]
        parts.append(
            f"<div style='margin: 18px 0 8px; padding: 6px 10px; "
            f"background: {lg.color}; color: white; border-radius: 6px; "
            f"display: inline-block; font-weight: 600;'>{lg.name}</div>"
        )
        parts.append("<ul style='list-style: none; padding: 0; margin: 0;'>")
        for e in es:
            t = _format_time(e["start_utc"])
            extras = []
            if e.get("broadcast"):
                extras.append(f"<span style='color: #6B7280;'>{e['broadcast']}</span>")
            if e.get("subtitle") and e["subtitle"] not in e["title"]:
                extras.append(f"<span style='color: #6B7280;'>{e['subtitle']}</span>")
            extra_html = " &middot; " + " &middot; ".join(extras) if extras else ""
            link_open = f"<a href='{e['url']}' style='color: #111827; text-decoration: none;'>" if e.get("url") else ""
            link_close = "</a>" if e.get("url") else ""
            parts.append(
                f"<li style='padding: 6px 0; border-bottom: 1px solid #F3F4F6;'>"
                f"<strong style='display: inline-block; width: 90px; color: #374151;'>{t}</strong>"
                f"{link_open}{e['title']}{link_close}{extra_html}"
                f"</li>"
            )
        parts.append("</ul>")
    parts.append("</body></html>")
    return "".join(parts)


def _hex_to_rgb(hex_color: str | None) -> tuple[int, int, int]:
    if not hex_color or not hex_color.startswith("#"):
        return (107, 114, 128)  # neutral gray fallback
    h = hex_color.lstrip("#")
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, IndexError):
        return (107, 114, 128)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Find a monospace TTF on macOS or Linux. Falls back to PIL's built-in
    pixel font (ugly but always available) if nothing matches."""
    if bold:
        candidates = [
            "/System/Library/Fonts/Menlo.ttc",  # macOS — Menlo Bold inside the ttc
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Menlo.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size, index=1 if (bold and path.endswith(".ttc")) else 0)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def build_digest_image(day_offset: int = 0) -> bytes:
    """Render today's agenda as a styled PNG. Each league name is drawn as a
    rounded colored pill (using the league's calendar color), columns are
    monospace-aligned, and the title sits above. Returns raw PNG bytes."""
    start, end, day = _sports_day_window_utc(day_offset)
    events = get_events(start_iso=start, end_iso=end)

    all_day = [e for e in events if e.get("all_day")]
    timed = sorted(
        [e for e in events if not e.get("all_day")],
        key=lambda e: e["start_utc"],
    )

    # Layout constants — kept high so Retina displays render crisply.
    WIDTH = 720
    PADDING = 28
    LINE_H = 30
    PILL_H = 24
    PILL_PAD = 10
    PILL_RADIUS = 6
    FONT_SIZE = 16

    font = _load_font(FONT_SIZE)
    bold = _load_font(FONT_SIZE, bold=True)
    big = _load_font(FONT_SIZE + 4, bold=True)

    # First pass: figure out the total height so the canvas is exactly the right size.
    rows = 1  # date header
    if all_day:
        rows += 1  # spacer
        rows += 1  # "All day:" header
        rows += len(all_day)
    if timed:
        rows += 1  # spacer
        rows += len(timed)
    if not events:
        rows += 2  # "No sports today" + spacer

    height = PADDING * 2 + rows * LINE_H + 8
    img = Image.new("RGB", (WIDTH, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    y = PADDING

    # Date header
    draw.text((PADDING, y), day.strftime("%A, %b %-d"), fill=(15, 23, 42), font=big)
    y += LINE_H + 6

    if not events:
        draw.text((PADDING, y), "No sports today.", fill=(100, 116, 139), font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def draw_pill(x: int, y: int, label: str, color_rgb: tuple[int, int, int]) -> int:
        """Draw a rounded colored pill with white centered text. Returns its right edge x."""
        text_w = draw.textlength(label, font=bold)
        pill_w = int(text_w + PILL_PAD * 2)
        draw.rounded_rectangle(
            (x, y, x + pill_w, y + PILL_H),
            radius=PILL_RADIUS,
            fill=color_rgb,
        )
        # Vertically center text inside the pill.
        text_y = y + (PILL_H - FONT_SIZE) // 2 - 1
        draw.text((x + PILL_PAD, text_y), label, fill=(255, 255, 255), font=bold)
        return x + pill_w

    if all_day:
        y += 4
        draw.text((PADDING, y), "All day", fill=(100, 116, 139), font=bold)
        y += LINE_H
        for e in all_day:
            lg = by_id(e["league"])
            tag = lg.name if lg else e["league"].upper()
            color = _hex_to_rgb(lg.color if lg else None)
            draw.text((PADDING, y + 4), "•  " + e["title"], fill=(15, 23, 42), font=font)
            # League pill anchored to the right edge.
            text_w = draw.textlength(tag, font=bold)
            pill_w = int(text_w + PILL_PAD * 2)
            draw_pill(WIDTH - PADDING - pill_w, y + 1, tag, color)
            y += LINE_H

    if timed:
        y += 8
        # Determine the widest matchup-title column so the league pills line up.
        max_title_w = 0
        for e in timed:
            extra = e.get("extra") or {}
            title = extra.get("short_name") or e["title"]
            w = int(draw.textlength(title, font=font))
            if w > max_title_w:
                max_title_w = w
        # Time column width — 6 chars at our font is plenty for "10:00a".
        time_col_w = int(draw.textlength("10:00am ", font=font))
        title_col_x = PADDING + time_col_w
        pill_col_x = title_col_x + max_title_w + 24  # gap before pill column

        for e in timed:
            t = _format_time_compact(e["start_utc"])
            lg = by_id(e["league"])
            tag = lg.name if lg else e["league"].upper()
            color = _hex_to_rgb(lg.color if lg else None)
            extra = e.get("extra") or {}
            title = extra.get("short_name") or e["title"]
            draw.text((PADDING, y + 4), t, fill=(100, 116, 139), font=font)
            draw.text((title_col_x, y + 4), title, fill=(15, 23, 42), font=font)
            draw_pill(pill_col_x, y + 1, tag, color)
            y += LINE_H

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_digest_sms(day_offset: int = 0) -> str:
    """Body for the daily push. Plain text: iOS ntfy renders this in
    monospace which keeps the columns aligned without us needing markdown
    (which iOS leaves as literal characters)."""
    start, end, day = _sports_day_window_utc(day_offset)
    events = get_events(start_iso=start, end_iso=end)
    header = day.strftime("%a %b %-d")

    if not events:
        return f"{header}\n\nNo sports today."

    all_day = [e for e in events if e.get("all_day")]
    timed = [e for e in events if not e.get("all_day")]
    timed.sort(key=lambda e: e["start_utc"])

    lines: list[str] = [header]

    # Column widths so the league column lines up vertically inside the
    # monospace code block.
    TIME_W, MATCH_W = 6, 14

    if all_day:
        lines.append("")
        lines.append("All day:")
        for e in all_day:
            lg = by_id(e["league"])
            tag = lg.name if lg else e["league"].upper()
            lines.append(f"• {e['title']}  ({tag})")

    if timed:
        lines.append("")
        # iOS ntfy uses a proportional font in the body and ignores markdown
        # code blocks reliably, so pixel-perfect column alignment isn't
        # achievable here. Use a single clean separator and let it flow.
        for e in timed:
            t = _format_time_compact(e["start_utc"])
            lg = by_id(e["league"])
            tag = lg.name if lg else e["league"].upper()
            extra = e.get("extra") or {}
            title = extra.get("short_name") or e["title"]
            lines.append(f"{t}  {title}  ({tag})")

    return "\n".join(lines)


# --- Senders ---

def _send_via_resend(to: str, subject: str, html: str, text: str) -> dict:
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        return {"sent": False, "reason": "no RESEND_API_KEY"}
    sender = os.getenv("RESEND_FROM", "onboarding@resend.dev")
    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": sender, "to": [to], "subject": subject, "html": html, "text": text},
        timeout=30.0,
    )
    return {"sent": r.is_success, "status": r.status_code, "body": r.text[:300]}


def _send_via_smtp(to: str, subject: str, html: str, text: str) -> dict:
    host = os.getenv("SMTP_HOST")
    if not host:
        return {"sent": False, "reason": "no SMTP_HOST"}
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    sender = os.getenv("SMTP_FROM", user)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        if user:
            s.login(user, password)
        s.sendmail(sender, [to], msg.as_string())
    return {"sent": True, "via": "smtp"}


def _send_ntfy(title: str, body: str, image: bytes | None = None) -> dict:
    """Push the digest to a ntfy.sh topic. No account or phone number needed —
    the topic name itself is the only secret, so pick something unguessable.

    If `image` is provided, the PNG is PUT as the message body (ntfy hosts it
    inline so iOS shows the styled image preview). The text body still goes
    in the `Message` header as accessibility / search fallback (single-line
    only — headers don't accept newlines, so we collapse it)."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return {"sent": False, "reason": "no NTFY_TOPIC configured"}
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": title,
        "Priority": "4",
    }
    token = os.getenv("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if image is not None:
        # ntfy file upload: PUT the bytes, set Filename so ntfy knows to host
        # it as an attachment. iOS shows the image preview inline.
        headers["Filename"] = "kcal-today.png"
        headers["Content-Type"] = "image/png"
        r = httpx.put(
            f"{server}/{topic}",
            headers=headers,
            content=image,
            timeout=30.0,
        )
    else:
        r = httpx.post(
            f"{server}/{topic}",
            headers=headers,
            content=body.encode("utf-8"),
            timeout=15.0,
        )
    return {"sent": r.is_success, "status": r.status_code, "topic": topic, "via": "ntfy"}


def _send_sms(body: str) -> dict:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("TWILIO_FROM")
    to = os.getenv("DIGEST_SMS_TO")
    if not all([sid, token, from_, to]):
        return {"sent": False, "reason": "twilio not configured"}
    r = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={"From": from_, "To": to, "Body": body[:1500]},
        timeout=30.0,
    )
    return {"sent": r.is_success, "status": r.status_code}


def send_digest() -> dict:
    """Build today's digest and dispatch via each configured channel.

    Channels are independent — SMS will still fire if email isn't
    configured (and vice-versa). Always writes a SMS-format preview to
    `data/last_digest_sms.txt` so the rendered body is inspectable even
    without Twilio set up."""
    sms_body = build_digest_sms()

    # Always write an SMS preview to disk for inspection.
    from pathlib import Path
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "last_digest_sms.txt").write_text(sms_body)

    # --- Email ---
    email_to = os.getenv("DIGEST_EMAIL_TO")
    if email_to:
        html = build_digest_html()
        text = build_digest_text()
        day = datetime.now(_local_tz()).strftime("%a %b %-d")
        subject = f"[Sports] {day}"
        if os.getenv("RESEND_API_KEY"):
            email_result = _send_via_resend(email_to, subject, html, text)
        elif os.getenv("SMTP_HOST"):
            email_result = _send_via_smtp(email_to, subject, html, text)
        else:
            out = data_dir / "last_digest.html"
            out.write_text(html)
            (data_dir / "last_digest.txt").write_text(text)
            email_result = {
                "sent": False,
                "reason": "no email provider configured",
                "preview_at": str(out),
            }
    else:
        email_result = {"sent": False, "reason": "no DIGEST_EMAIL_TO configured"}

    # --- SMS ---
    if all(os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "DIGEST_SMS_TO")):
        sms_result = _send_sms(sms_body)
    else:
        sms_result = {
            "sent": False,
            "reason": "twilio not configured",
            "preview_at": str(data_dir / "last_digest_sms.txt"),
        }

    # --- ntfy.sh push ---
    # Plain hyphen, not em-dash: ntfy headers must be ASCII-encodable.
    ntfy_title = "K-Cal - " + datetime.now(_local_tz()).strftime("%a %b %-d")
    # Still render the styled PNG for local preview / future use, but send
    # the text body as the push. iOS ntfy free tier doesn't render inline
    # image previews — file uploads display as "You received a file" with
    # no body text, which is a worse glanceable experience than plain text.
    try:
        (data_dir / "last_digest.png").write_bytes(build_digest_image())
    except Exception as exc:
        print(f"image render failed (preview only, push unaffected): {exc}")
    ntfy_result = _send_ntfy(ntfy_title, sms_body)

    return {"email": email_result, "sms": sms_result, "ntfy": ntfy_result}
