"""Mailer with two transports.

1. Brevo HTTP API (``BREVO_API_KEY``) — preferred on hosts that block outbound
   SMTP ports, such as Render's free tier.
2. Plain SMTP (``SMTP_HOST`` etc.) — Gmail, Resend, Postmark, any relay.

When neither is configured (local dev) emails are printed to the console so the
auth flows are testable without a mail account.
"""
import logging
import re
import smtplib
from email.message import EmailMessage

import requests
from flask import current_app

log = logging.getLogger(__name__)


def _strip_env_quotes(value: str) -> str:
    """Render/dashboard pastes often wrap secrets in quotes — strip them."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _brevo_api_key() -> str:
    """Normalize the Brevo key (strip whitespace / quotes / Bearer prefix)."""
    key = _strip_env_quotes(current_app.config.get("BREVO_API_KEY") or "")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def _parse_from(mail_from: str) -> dict:
    """Split 'Name <addr@x.com>' into Brevo's {"name": ..., "email": ...}."""
    mail_from = _strip_env_quotes(mail_from or "")
    match = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", mail_from)
    if match:
        name, email = match.groups()
        return {"name": (name or "Bloom Anyway").strip() or "Bloom Anyway",
                "email": email.strip()}
    if "@" in mail_from:
        return {"name": "Bloom Anyway", "email": mail_from}
    return {"name": "Bloom Anyway", "email": mail_from}


def _send_via_brevo(to: str, subject: str, text_body: str) -> bool:
    """Send a plain-text email through Brevo. Kept simple on purpose."""
    key = _brevo_api_key()
    if not key:
        return False
    sender = _parse_from(current_app.config.get("MAIL_FROM") or "")
    if not sender.get("email") or "@" not in sender["email"]:
        log.error("Brevo: MAIL_FROM is missing a real email address (got %r). "
                  "Set MAIL_FROM to a sender verified in your Brevo account.",
                  current_app.config.get("MAIL_FROM"))
        return False
    if sender["email"].endswith("@localhost"):
        log.error("Brevo: MAIL_FROM still uses @localhost — set it to a verified sender.")
        return False

    payload = {
        "sender": sender,
        "to": [{"email": to}],
        "subject": subject,
        "textContent": text_body,
    }
    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={
                "api-key": key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=20,
        )
        if resp.status_code in (200, 201, 202):
            log.info("Brevo: sent to %s (status %s)", to, resp.status_code)
            return True
        log.error("Brevo rejected email to %s: %s %s", to, resp.status_code, resp.text)
        # Common fix: sender not verified — retry with email-only sender name stripped
        if resp.status_code == 400 and "name" in payload["sender"]:
            payload["sender"] = {"email": sender["email"]}
            resp2 = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={
                    "api-key": key,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                timeout=20,
            )
            if resp2.status_code in (200, 201, 202):
                log.info("Brevo: sent to %s on retry (status %s)", to, resp2.status_code)
                return True
            log.error("Brevo retry failed for %s: %s %s", to, resp2.status_code, resp2.text)
        return False
    except Exception:
        log.exception("Failed to reach Brevo API for email to %s", to)
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
        return True
    except Exception:
        log.exception("Failed to send email to %s via SMTP", to)
        return False


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    """Send email. Verification codes use text only; html_body is ignored for Brevo
    to keep delivery as reliable as possible."""
    cfg = current_app.config
    to = (to or "").strip()
    if not to:
        return False

    if _brevo_api_key():
        return _send_via_brevo(to, subject, text_body)

    if not cfg["SMTP_HOST"]:
        log.warning("No email transport configured; printing email to console.")
        print("\n===== EMAIL (console fallback) =====")
        print(f"To: {to}\nSubject: {subject}\n\n{text_body}")
        print("====================================\n")
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = to
    msg.set_content(text_body)
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
        return False
    text = f"New message from the contact form.\n\nFrom: {name} <{email}>\n\n{body}"
    return send_email(admin, f"Contact form: {name}", text)
