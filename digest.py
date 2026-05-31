"""Daily digest builder + sender.

Builds a plain-text and HTML version of "today's sports" grouped by league, in
the user's local timezone. Sends via Resend (preferred) or SMTP. Optionally
sends a shortened SMS via Twilio.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

import httpx
import pytz

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


def _format_time(iso: str) -> str:
    tz = _local_tz()
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%-I:%M %p %Z")
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


def build_digest_sms(day_offset: int = 0) -> str:
    """Shorter version for SMS (Twilio splits >160 chars, so keep it tight)."""
    start, end, day = _day_window_utc(day_offset)
    events = get_events(start_iso=start, end_iso=end)
    if not events:
        return f"{day.strftime('%a %b %-d')}: no sports today."
    grouped = _group_by_league(events)
    parts = [day.strftime("%a %b %-d") + ":"]
    for lg in LEAGUES:
        if lg.id not in grouped:
            continue
        parts.append(f"{lg.name}({len(grouped[lg.id])})")
    return " ".join(parts)


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
    """Build today's digest and send it via configured channels."""
    to = os.getenv("DIGEST_EMAIL_TO")
    if not to:
        return {"sent": False, "reason": "no DIGEST_EMAIL_TO configured"}
    html = build_digest_html()
    text = build_digest_text()
    day = datetime.now(_local_tz()).strftime("%a %b %-d")
    subject = f"[Sports] {day}"

    if os.getenv("RESEND_API_KEY"):
        email_result = _send_via_resend(to, subject, html, text)
    elif os.getenv("SMTP_HOST"):
        email_result = _send_via_smtp(to, subject, html, text)
    else:
        # Fallback: write to a file so the user can see what would have sent
        from pathlib import Path
        out = Path(__file__).parent / "data" / "last_digest.html"
        out.write_text(html)
        (out.parent / "last_digest.txt").write_text(text)
        email_result = {"sent": False, "reason": "no email provider configured", "preview_at": str(out)}

    sms_result = _send_sms(build_digest_sms()) if os.getenv("TWILIO_ACCOUNT_SID") else {"sent": False, "reason": "sms not configured"}
    return {"email": email_result, "sms": sms_result}
