"""Configuration classes, read from environment variables.

Local development works with zero configuration (SQLite + console email).
Production (APP_ENV=production) refuses to boot with missing secrets.
"""
import os
from datetime import timedelta


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///firstlight-dev.db"
    # Render (and Heroku) hand out postgres:// which SQLAlchemy 2.x rejects.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    # Optional: if unset, a persistent key is generated and stored in the
    # database on first boot (see app factory), so it survives restarts
    # without needing an env var.
    SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Managed Postgres (e.g. Render) drops idle connections; without pre-ping the
    # first request after an idle spell hits a dead connection and 500s
    # ("something went sideways"). Pre-ping + recycle keeps the pool healthy.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    # Sessions / auth
    SESSION_COOKIE_NAME = "firstlight_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_NAME = "firstlight_remember"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    CODE_MAX_AGE_MINUTES = 15
    ADMIN_FRESH_LOGIN_HOURS = 24

    # Email — two transports, first configured one wins:
    # 1. BREVO_API_KEY: HTTP API (works on hosts that block SMTP, e.g. Render free tier)
    # 2. SMTP_*: classic SMTP relay (Gmail, Resend, Postmark, any)
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "First Light <hello@localhost>")

    # Lemon Squeezy (both optional: the storefront works before payments are
    # wired. Webhooks are rejected until the secret is set; the "Sync" button
    # needs the API key.)
    LEMONSQUEEZY_API_KEY = os.environ.get("LEMONSQUEEZY_API_KEY", "")
    LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")

    # Flask-Limiter: in-memory storage. Fine at this scale; counters reset on
    # deploy/restart (noted in README).
    RATELIMIT_STORAGE_URI = "memory://"


class DevConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False
    # Zero-config local dev: a fixed dev key unless one is provided.
    SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or "dev-only-not-secret"


class ProdConfig(Config):
    DEBUG = False
    PREFERRED_URL_SCHEME = "https"
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

    #: the only env vars that must be present in prod (everything else is
    #: optional or auto-managed)
    REQUIRED_ENV = (
        "DATABASE_URL",
        "MAIL_FROM",
    )
    #: at least one email transport must be configured
    SMTP_ENV = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")

    @classmethod
    def validate(cls) -> None:
        def unset(name):
            return os.environ.get(name, "").strip() == ""

        missing = [name for name in cls.REQUIRED_ENV if unset(name)]
        if unset("BREVO_API_KEY") and any(unset(name) for name in cls.SMTP_ENV):
            missing.append("BREVO_API_KEY or all of SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD")
        if missing:
            raise RuntimeError(
                "Refusing to start in production. Missing/placeholder env vars: "
                + ", ".join(missing)
            )
        # A SQLite database in production lives on an ephemeral disk (wiped on
        # every restart/deploy), which silently loses the owner account, orders,
        # etc. Force a real, persistent database.
        if cls.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
            raise RuntimeError(
                "Refusing to start in production with a SQLite database — it is "
                "not persistent. Attach a managed Postgres database and set "
                "DATABASE_URL to its connection string."
            )


def get_config():
    env = os.environ.get("APP_ENV", "").lower()
    # Render sets RENDER=true on every service. If APP_ENV wasn't set explicitly
    # we still force production there, so the app uses the managed (persistent)
    # Postgres via DATABASE_URL instead of ephemeral SQLite — otherwise the disk
    # is wiped on every restart/deploy and the owner account "resets".
    if not env and os.environ.get("RENDER"):
        env = "production"
    if not env:
        env = "development"
    if env == "production":
        ProdConfig.validate()
        return ProdConfig
    return DevConfig
