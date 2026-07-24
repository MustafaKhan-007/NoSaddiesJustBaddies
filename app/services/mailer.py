"""Mailer with two transports.

1. Resend HTTP API (``RESEND_API_KEY``) — preferred on hosts that block outbound
   SMTP ports (e.g. Render free tier).
2. Plain SMTP (``SMTP_HOST`` etc.) — any relay, only when Resend is unset.

When neither is configured (local dev) emails are printed to the console so the
auth flows are testable without a mail account.
"""
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from html import escape

import requests
from flask import current_app

log = logging.getLogger(__name__)

RESEND_SEND_URL = "https://api.resend.com/emails"

# Most recent send failure (human-readable). Cleared on success.
_last_error = ""


def last_send_error() -> str:
    return _last_error


def _set_error(message: str) -> None:
    global _last_error
    _last_error = (message or "").strip()


def _strip_env_quotes(value: str) -> str:
    """Render/dashboard pastes often wrap secrets in quotes — strip them."""
    v = (value or "").strip().lstrip("\ufeff")
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _resend_api_key() -> str:
    """Normalize the Resend API key (env first, then app config)."""
    raw = os.environ.get("RESEND_API_KEY")
    if raw is None or not str(raw).strip():
        raw = current_app.config.get("RESEND_API_KEY") or ""
    key = _strip_env_quotes(str(raw))
    key = re.sub(r"\s+", "", key)
    lower = key.lower()
    if lower.startswith("bearer"):
        key = key[6:].lstrip(":").strip()
        lower = key.lower()
    for prefix in ("api-key:", "apikey:", "x-api-key:"):
        if lower.startswith(prefix):
            key = key[len(prefix):].strip()
            break
    return key


def _mail_from() -> str:
    raw = os.environ.get("MAIL_FROM")
    if raw is None or not str(raw).strip():
        raw = current_app.config.get("MAIL_FROM") or ""
    return _strip_env_quotes(str(raw))


def _resend_error_hint(status: int, body: str) -> str:
    """Turn a Resend HTTP failure into a short owner-facing hint."""
    text = (body or "").lower()
    if status == 401 or "unauthorized" in text or "invalid api key" in text:
        return (
            "Resend rejected the API key (401). Set RESEND_API_KEY to a key "
            "from Resend → API Keys (usually starts with re_)."
        )
    if status == 403:
        return (
            "Resend forbade the send (403). Check your Resend plan/limits and "
            "that the domain for MAIL_FROM is verified."
        )
    if status == 422 or "validation" in text or "from" in text:
        return (
            "Resend rejected the sender or payload. MAIL_FROM must use a "
            "verified domain in Resend (or onboarding@resend.dev for testing). "
            f"Details: {(body or '')[:220]}"
        )
    if status == 429:
        return "Resend rate limit hit — wait a minute and try again."
    return f"Resend error {status}: {(body or '')[:240]}"


def _send_via_resend(to: str, subject: str, text_body: str) -> bool:
    """Send through Resend's HTTP API."""
    key = _resend_api_key()
    if not key:
        _set_error("RESEND_API_KEY is empty on the server. Set it in Render and redeploy.")
        return False

    mail_from = _mail_from()
    if not mail_from or "@" not in mail_from or "@localhost" in mail_from.lower():
        log.error("Resend: MAIL_FROM is missing a real email address (got %r).", mail_from)
        _set_error(
            "MAIL_FROM must be a real address on a Resend-verified domain, "
            "e.g. Bloom Anyway <hello@yourdomain.com>."
        )
        return False

    html_body = (
        "<pre style=\"font-family:ui-monospace,monospace;white-space:pre-wrap;"
        "font-size:15px;line-height:1.5;\">"
        f"{escape(text_body)}</pre>"
    )
    payload = {
        "from": mail_from,
        "to": [to],
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    try:
        resp = requests.post(
            RESEND_SEND_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=20,
        )
        if resp.status_code in (200, 201):
            log.info("Resend: sent to %s (status %s)", to, resp.status_code)
            _set_error("")
            return True
        hint = _resend_error_hint(resp.status_code, resp.text)
        log.error("Resend rejected email to %s: %s %s", to, resp.status_code, resp.text)
        _set_error(hint)
        return False
    except Exception as exc:
        log.exception("Failed to reach Resend API for email to %s", to)
        _set_error(f"Could not reach Resend ({exc.__class__.__name__}).")
        return False


def _send_via_smtp(to: str, msg: EmailMessage) -> bool:
    cfg = current_app.config
    try:
        if int(cfg["SMTP_PORT"]) == 465:
            server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15)
        else:
            server = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15)
            server.starttls()
        with server:
            if cfg["SMTP_USER"]:
                server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.send_message(msg)
        _set_error("")
        return True
    except Exception as exc:
        log.exception("Failed to send email to %s via SMTP", to)
        _set_error(f"SMTP send failed ({exc.__class__.__name__}).")
        return False


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    """Send email. Prefer Resend; fall back to SMTP; else console in local dev."""
    cfg = current_app.config
    to = (to or "").strip()
    if not to:
        _set_error("Missing recipient email.")
        return False

    if _resend_api_key():
        return _send_via_resend(to, subject, text_body)

    if not cfg["SMTP_HOST"]:
        log.warning("No email transport configured; printing email to console.")
        print("\n===== EMAIL (console fallback) =====")
        print(f"To: {to}\nSubject: {subject}\n\n{text_body}")
        print("====================================\n")
        _set_error("")
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _mail_from()
    msg["To"] = to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return _send_via_smtp(to, msg)


def send_verification_code(to: str, code: str, purpose: str) -> bool:
    minutes = current_app.config["CODE_MAX_AGE_MINUTES"]
    if purpose == "reset":
        subject = "Your password reset code"
        intro = "Here's your code to reset your password:"
    else:
        subject = "Your confirmation code"
        intro = "Welcome. Here's your code to confirm your email:"
    text = (
        f"{intro}\n\n    {code}\n\n"
        f"It expires in {minutes} minutes.\n"
        "If you didn't request it, you can safely ignore this email.\n\n"
        "— Bloom Anyway"
    )
    return send_email(to, subject, text)


def send_contact_notification(name: str, email: str, body: str) -> bool:
    from ..models import User
    owner = (User.query.filter_by(is_admin=True)
             .filter(User.deleted_at.is_(None)).order_by(User.id).first())
    admin = owner.email if owner else None
    if not admin:
        log.warning("No owner account to notify; contact message stored but not emailed.")
        _set_error("No owner account email to notify.")
        return False
    text = f"New message from the contact form.\n\nFrom: {name} <{email}>\n\n{body}"
    return send_email(admin, f"Contact form: {name}", text)
