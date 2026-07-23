"""Mailer with two transports.

1. Brevo HTTP API (``BREVO_API_KEY``) — preferred on hosts that block outbound
   SMTP ports, such as Render's free tier.
2. Plain SMTP (``SMTP_HOST`` etc.) — Gmail, Resend, Postmark, any relay.

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

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"

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


def _brevo_api_key() -> str:
    """Normalize the Brevo API key (env first, then app config)."""
    # Read live env so a Render restart always picks up the dashboard value.
    raw = os.environ.get("BREVO_API_KEY")
    if raw is None or not str(raw).strip():
        raw = current_app.config.get("BREVO_API_KEY") or ""
    key = _strip_env_quotes(str(raw))
    # Copy-paste sometimes inserts spaces/newlines inside the key.
    key = re.sub(r"\s+", "", key)
    lower = key.lower()
    if lower.startswith("bearer"):
        key = key[6:].lstrip(":").strip()
        lower = key.lower()
    for prefix in ("api-key:", "apikey:", "x-api-key:"):
        if lower.startswith(prefix):
            key = key[len(prefix):].strip()
            lower = key.lower()
            break
    return key


def _mail_from() -> str:
    raw = os.environ.get("MAIL_FROM")
    if raw is None or not str(raw).strip():
        raw = current_app.config.get("MAIL_FROM") or ""
    return _strip_env_quotes(str(raw))


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


def _brevo_error_hint(status: int, body: str) -> str:
    """Turn a Brevo HTTP failure into a short owner-facing hint."""
    text = (body or "").lower()
    if status == 401 or "unauthorized" in text or "not verified" in text:
        return (
            "Brevo blocked the API call (unauthorized / IP not verified). "
            "This usually happens after you authorize a home/computer IP — "
            "that turns on blocking and locks out Render. Fix: Brevo → "
            "Settings → Security → Authorized IPs → Deactivate blocking for "
            "API (or open Brevo's authorization email and approve Render's IP). "
            "Also confirm BREVO_API_KEY is an API key (xkeysib-…), not an SMTP key."
        )
    if status == 403:
        return (
            "Brevo forbade the send (403). Check that transactional email is "
            "enabled and the sender/domain are verified for this Brevo account."
        )
    if status == 400 and ("sender" in text or "from" in text):
        return (
            "Brevo rejected the sender. MAIL_FROM must be an exact verified "
            "sender address on this Brevo account (domain authenticated)."
        )
    if status == 400:
        return f"Brevo rejected the email (400): {(body or '')[:240]}"
    return f"Brevo error {status}: {(body or '')[:240]}"


def _post_brevo(payload: dict, key: str):
    return requests.post(
        BREVO_SEND_URL,
        json=payload,
        headers={
            "api-key": key,
            "accept": "application/json",
            "content-type": "application/json",
        },
        timeout=20,
    )


def _send_via_brevo(to: str, subject: str, text_body: str) -> bool:
    """Send through Brevo HTTP API."""
    key = _brevo_api_key()
    if not key:
        _set_error("BREVO_API_KEY is empty on the server. Set it in Render and redeploy.")
        return False
    if key.lower().startswith("xsmtpsib-"):
        log.error("Brevo: BREVO_API_KEY looks like an SMTP key (xsmtpsib-…), "
                  "not an API key (xkeysib-…).")
        _set_error(
            "BREVO_API_KEY is an SMTP key (xsmtpsib-…). Create an API key in "
            "Brevo → SMTP & API → API keys (xkeysib-…) and paste that instead."
        )
        return False
    if not key.lower().startswith("xkeysib-"):
        log.warning("Brevo: API key does not start with xkeysib- (got prefix %r)",
                    key[:12])

    sender = _parse_from(_mail_from())
    if not sender.get("email") or "@" not in sender["email"]:
        log.error("Brevo: MAIL_FROM is missing a real email address (got %r).",
                  _mail_from())
        _set_error("MAIL_FROM must be a real verified email, e.g. "
                   "Bloom Anyway <hello@yourdomain.com>.")
        return False
    if sender["email"].endswith("@localhost"):
        log.error("Brevo: MAIL_FROM still uses @localhost.")
        _set_error("MAIL_FROM still uses @localhost — set a verified Brevo sender.")
        return False

    html_body = (
        "<pre style=\"font-family:ui-monospace,monospace;white-space:pre-wrap;"
        "font-size:15px;line-height:1.5;\">"
        f"{escape(text_body)}</pre>"
    )
    payload = {
        "sender": sender,
        "to": [{"email": to}],
        "subject": subject,
        "textContent": text_body,
        "htmlContent": html_body,
    }
    try:
        resp = _post_brevo(payload, key)
        if resp.status_code in (200, 201, 202):
            log.info("Brevo: sent to %s (status %s)", to, resp.status_code)
            _set_error("")
            return True

        hint = _brevo_error_hint(resp.status_code, resp.text)
        log.error("Brevo rejected email to %s: %s %s", to, resp.status_code, resp.text)

        # Retry once with email-only sender (some accounts dislike custom names).
        if resp.status_code == 400 and "name" in payload.get("sender", {}):
            payload["sender"] = {"email": sender["email"]}
            resp2 = _post_brevo(payload, key)
            if resp2.status_code in (200, 201, 202):
                log.info("Brevo: sent to %s on retry (status %s)", to, resp2.status_code)
                _set_error("")
                return True
            hint = _brevo_error_hint(resp2.status_code, resp2.text)
            log.error("Brevo retry failed for %s: %s %s", to, resp2.status_code, resp2.text)

        _set_error(hint)
        return False
    except Exception as exc:
        log.exception("Failed to reach Brevo API for email to %s", to)
        _set_error(f"Could not reach Brevo ({exc.__class__.__name__}).")
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
    """Send email. Verification codes use text (+ tiny HTML mirror) via Brevo."""
    cfg = current_app.config
    to = (to or "").strip()
    if not to:
        _set_error("Missing recipient email.")
        return False

    if _brevo_api_key():
        return _send_via_brevo(to, subject, text_body)

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
