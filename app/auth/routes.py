"""Email + password authentication with 6-digit email confirmation codes.

Security properties:
- Passwords hashed with werkzeug (scrypt), min 8 chars, never logged.
- Confirmation/reset codes: 6 random digits, only the SHA-256 hash stored,
  15-minute expiry, single-use, max 5 wrong attempts per code.
- Rate limits on login, registration, and code resends.
- Password reset uses a uniform response (no account enumeration).
- `next` restricted to relative paths (no open redirects).
"""
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta

from flask import (abort, current_app, flash, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db, limiter
from ..models import User, VerificationCode, utcnow
from ..services.mailer import send_verification_code
from ..services.recommend import INTENTS, valid_intent_keys
from . import bp

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _password(form_field: str = "password") -> str:
    """Trim surrounding whitespace — copy-pasted passwords often carry a
    trailing space/newline, and a leading/trailing blank is never intended."""
    return (request.form.get(form_field) or "").strip()


def _email_key():
    return _normalize(request.form.get("email", "")) or (request.remote_addr or "ip")


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("main.account")


def _issue_code(user: User, purpose: str) -> bool:
    """Create a fresh code (invalidating older ones for the purpose) and email it.

    Returns False when the email could not be sent (SMTP down/misconfigured).
    """
    VerificationCode.query.filter_by(user_id=user.id, purpose=purpose, used_at=None)\
        .update({"used_at": utcnow()})
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.session.add(VerificationCode(
        user_id=user.id,
        code_hash=hashlib.sha256(code.encode()).hexdigest(),
        purpose=purpose,
        expires_at=utcnow() + timedelta(minutes=current_app.config["CODE_MAX_AGE_MINUTES"]),
        request_ip=request.remote_addr,
    ))
    db.session.commit()
    sent = send_verification_code(user.email, code, purpose)
    log.info("auth: %s code issued for user %s (sent=%s, ip=%s)",
             purpose, user.id, sent, request.remote_addr)
    return sent


EMAIL_TROUBLE = ("We couldn't send the email just now \u2014 the site's email service "
                 "didn't respond. Please try again in a few minutes.")


def _check_code(user: User, purpose: str, submitted: str) -> tuple[bool, str]:
    """Validate a submitted code. Returns (ok, error_message)."""
    submitted = (submitted or "").strip().replace(" ", "")
    row = (VerificationCode.query
           .filter_by(user_id=user.id, purpose=purpose, used_at=None)
           .order_by(VerificationCode.created_at.desc()).first())
    if row is None or row.expires_at <= utcnow():
        return False, "That code has expired. Send yourself a fresh one below."
    if row.attempts >= VerificationCode.MAX_ATTEMPTS:
        return False, "Too many tries with that code. Send yourself a fresh one below."
    if hashlib.sha256(submitted.encode()).hexdigest() != row.code_hash:
        row.attempts += 1
        db.session.commit()
        left = VerificationCode.MAX_ATTEMPTS - row.attempts
        if left <= 0:
            return False, "Too many tries with that code. Send yourself a fresh one below."
        return False, f"That code doesn't match \u2014 check the email again ({left} tries left)."
    row.used_at = utcnow()
    db.session.commit()
    return True, ""


def _log_in(user: User, remember: bool = True):
    user.last_login_at = utcnow()
    # honour any membership bought before this account existed / signed in
    from ..services.memberships import reconcile_user
    reconcile_user(user)
    db.session.commit()
    login_user(user, remember=remember)
    # permanent session so the admin idle-timeout window survives browser restarts
    session.permanent = True
    now = datetime.utcnow().isoformat()
    session["logged_in_at"] = now
    session["admin_seen_at"] = now
    log.info("auth: user %s logged in (ip=%s)", user.id, request.remote_addr)


# ============================ FIRST-RUN SETUP ================================

def _setup_available() -> bool:
    """The owner account can be claimed until an admin has logged in once.

    This lets the site be set up entirely in the browser (no env vars, no
    shell) and survives redeploys: once the owner has signed in a single
    time, /setup locks itself forever.
    """
    claimed = User.query.filter(
        User.is_admin.is_(True), User.last_login_at.isnot(None)
    ).first()
    return claimed is None


@bp.route("/setup", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def setup():
    if not _setup_available():
        abort(404)
    if request.method == "GET":
        return render_template("auth/setup.html")

    email = _normalize(request.form.get("email"))
    password = _password()
    if not EMAIL_RE.match(email) or len(email) > 255:
        flash("That doesn't look like an email address \u2014 mind checking it?", "error")
        return render_template("auth/setup.html", email=request.form.get("email", "")), 400
    if len(password) < MIN_PASSWORD_LEN:
        flash(f"Your password needs at least {MIN_PASSWORD_LEN} characters.", "error")
        return render_template("auth/setup.html", email=email), 400

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email)
        db.session.add(user)
    user.set_password(password)
    user.is_admin = True
    user.email_verified_at = user.email_verified_at or utcnow()
    user.deleted_at = None
    db.session.commit()

    _log_in(user)
    log.info("auth: owner account claimed by user %s (ip=%s)", user.id, request.remote_addr)
    flash("Welcome \u2014 this is your studio. The setup page is now locked.", "success")
    return redirect(url_for("admin.dashboard"))


# ================================ REGISTER ===================================

@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.account"))
    if request.method == "GET":
        return render_template("auth/register.html", next=request.args.get("next", ""),
                               intents=INTENTS, selected_goals=set())

    email = _normalize(request.form.get("email"))
    password = _password()
    next_path = request.form.get("next", "")
    goals = valid_intent_keys(request.form.getlist("goals"))

    errors = []
    if not EMAIL_RE.match(email) or len(email) > 255:
        errors.append("That doesn't look like an email address \u2014 mind checking it?")
    if len(password) < MIN_PASSWORD_LEN:
        errors.append(f"Your password needs at least {MIN_PASSWORD_LEN} characters.")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("auth/register.html", next=next_path,
                               email=request.form.get("email", ""),
                               intents=INTENTS, selected_goals=set(goals)), 400

    existing = User.query.filter_by(email=email).first()
    if existing and existing.is_verified:
        flash("You already have an account \u2014 sign in below (or reset your password if it's slipped away).", "info")
        return redirect(url_for("auth.login", next=next_path))
    if existing:
        # unverified leftover registration: refresh the password and re-send the code
        user = existing
        user.set_password(password)
    else:
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
    if goals:
        user.set_goals(goals)
    db.session.commit()

    sent = _issue_code(user, "confirm")
    session["pending_verify_email"] = email
    session["auth_next"] = next_path
    if sent:
        flash("One more step \u2014 we've emailed you a 6-digit code.", "success")
    else:
        flash(EMAIL_TROUBLE, "error")
    return redirect(url_for("auth.verify_email"))


@bp.route("/verify-email", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def verify_email():
    email = _normalize(request.form.get("email")) or session.get("pending_verify_email", "")
    if request.method == "GET":
        return render_template("auth/verify_email.html", email=email)

    user = User.query.filter_by(email=email).first() if email else None
    if user is None or user.deleted_at is not None:
        flash("We couldn't find that address \u2014 try registering again.", "error")
        return redirect(url_for("auth.register"))

    if request.form.get("action") == "resend":
        if _issue_code(user, "confirm"):
            flash("A fresh code is on its way. It works for 15 minutes.", "success")
        else:
            flash(EMAIL_TROUBLE, "error")
        return render_template("auth/verify_email.html", email=email)

    if user.is_verified:
        flash("You're already confirmed \u2014 just sign in.", "info")
        return redirect(url_for("auth.login"))

    ok, error = _check_code(user, "confirm", request.form.get("code"))
    if not ok:
        flash(error, "error")
        return render_template("auth/verify_email.html", email=email), 400

    user.email_verified_at = utcnow()
    db.session.commit()
    _log_in(user)
    session.pop("pending_verify_email", None)
    next_path = session.pop("auth_next", "")
    flash("Welcome in. Your account is confirmed.", "success")
    return redirect(_safe_next(next_path or None))


# ================================= LOGIN =====================================

@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
@limiter.limit("5 per minute", key_func=_email_key, methods=["POST"])
def login():
    if request.method == "GET":
        if current_user.is_authenticated:
            return redirect(url_for("main.account"))
        return render_template("auth/login.html", next=request.args.get("next", ""),
                               setup_available=_setup_available())

    email = _normalize(request.form.get("email"))
    password = _password()
    next_path = request.form.get("next", "")

    user = User.query.filter_by(email=email).first()
    if user is None or user.deleted_at is not None or not user.check_password(password):
        log.info("auth: failed login (ip=%s)", request.remote_addr)
        flash("That email and password don't match. Take a breath and try again \u2014 or reset your password below.", "error")
        return render_template("auth/login.html", next=next_path,
                               email=request.form.get("email", ""),
                               setup_available=_setup_available()), 401

    if not user.is_verified:
        sent = _issue_code(user, "confirm")
        session["pending_verify_email"] = user.email
        session["auth_next"] = next_path
        if sent:
            flash("Almost there \u2014 confirm your email first. We've sent you a fresh code.", "info")
        else:
            flash(EMAIL_TROUBLE, "error")
        return redirect(url_for("auth.verify_email"))

    _log_in(user)
    if next_path:
        return redirect(_safe_next(next_path))
    # admins land in the studio, everyone else in their space
    return redirect(url_for("admin.dashboard") if user.is_admin else url_for("main.account"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.pop("logged_in_at", None)
    flash("You're signed out. Come back any time.", "success")
    return redirect(url_for("main.index"))


# ============================ PASSWORD RESET =================================

UNIFORM_RESET_MESSAGE = ("If that address has an account, a 6-digit reset code is on its way. "
                         "It works for 15 minutes \u2014 check spam too.")


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
@limiter.limit("3 per hour", key_func=_email_key, methods=["POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("auth/forgot_password.html")

    email = _normalize(request.form.get("email"))
    user = User.query.filter_by(email=email).first()
    if user and user.deleted_at is None:
        _issue_code(user, "reset")
    else:
        log.info("auth: reset requested for unknown email (ip=%s)", request.remote_addr)
    session["pending_reset_email"] = email
    flash(UNIFORM_RESET_MESSAGE, "success")
    return redirect(url_for("auth.reset_password"))


@bp.route("/reset-password", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def reset_password():
    email = _normalize(request.form.get("email")) or session.get("pending_reset_email", "")
    if request.method == "GET":
        return render_template("auth/reset_password.html", email=email)

    password = _password()
    if len(password) < MIN_PASSWORD_LEN:
        flash(f"Your new password needs at least {MIN_PASSWORD_LEN} characters.", "error")
        return render_template("auth/reset_password.html", email=email), 400

    user = User.query.filter_by(email=email).first() if email else None
    if user is None or user.deleted_at is not None:
        # uniform: don't reveal whether the account exists
        flash("That code doesn't match \u2014 check the email again.", "error")
        return render_template("auth/reset_password.html", email=email), 400

    ok, error = _check_code(user, "reset", request.form.get("code"))
    if not ok:
        flash(error, "error")
        return render_template("auth/reset_password.html", email=email), 400

    user.set_password(password)
    if not user.is_verified:      # proving inbox ownership verifies the email too
        user.email_verified_at = utcnow()
    db.session.commit()
    session.pop("pending_reset_email", None)
    _log_in(user)
    flash("New password saved. You're in.", "success")
    return redirect(url_for("main.account"))
