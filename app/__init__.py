"""First Light — app factory."""
import logging
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, request  # noqa: E402
from sqlalchemy import text  # noqa: E402

from .config import get_config  # noqa: E402
from .extensions import csrf, db, limiter, login_manager, migrate  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://assets.lemonsqueezy.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' https: data:; "
    "frame-src 'self' https://*.lemonsqueezy.com https://app.lemonsqueezy.com; "
    "connect-src 'self' https://*.lemonsqueezy.com; "
    "base-uri 'self'; form-action 'self' https://*.lemonsqueezy.com; "
    "frame-ancestors 'none'"
)


def _ensure_secret_key(app):
    """Use SECRET_KEY from config if set; otherwise fall back to a persistent
    key stored in the database (generated once). Only touches the DB when no
    key was provided, and degrades to a temporary key if the DB is unreachable."""
    if app.config.get("SECRET_KEY"):
        return
    from .services.settings import get_or_create_secret_key
    with app.app_context():
        try:
            app.config["SECRET_KEY"] = get_or_create_secret_key()
        except Exception:
            db.session.rollback()   # leave the session clean for e.g. `flask db upgrade`
            import secrets
            app.config["SECRET_KEY"] = secrets.token_hex(32)
            logging.getLogger(__name__).warning(
                "Could not load a persistent SECRET_KEY from the database "
                "(is it migrated yet?); using a temporary key for this process."
            )


def create_app(config_class=None):
    app = Flask(__name__)
    app.config.from_object(config_class or get_config())

    db.init_app(app)
    migrate.init_app(app, db)
    _ensure_secret_key(app)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Sign in to keep going."

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        if user and user.deleted_at is None:
            return user
        return None

    # --- blueprints ----------------------------------------------------------
    from .auth import bp as auth_bp
    from .main import bp as main_bp
    from .admin import bp as admin_bp
    from .webhooks import bp as webhooks_bp
    from .forums import bp as forums_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(webhooks_bp, url_prefix="/webhooks")
    app.register_blueprint(forums_bp, url_prefix="/forums")
    csrf.exempt(webhooks_bp)  # webhook signature check replaces CSRF here

    # --- template globals / filters ------------------------------------------
    from markupsafe import Markup, escape

    from .services.markdown import render_markdown
    from .services.settings import active_announcement, all_settings

    app.jinja_env.filters["markdown"] = render_markdown

    def nl2br(value):
        """Escape user text, then turn newlines into <br> for safe display."""
        escaped = escape(value or "")
        return Markup(str(escaped).replace("\n", "<br>\n"))

    app.jinja_env.filters["nl2br"] = nl2br

    from .services import badges as badges_service

    app.jinja_env.globals.update(
        primary_badge=badges_service.primary_badge,
        profile_badges=badges_service.profile_badges,
    )

    @app.context_processor
    def inject_globals():
        return {"site": all_settings(), "announcement": active_announcement(),
                "current_year": date.today().year}

    # --- health check ---------------------------------------------------------
    @app.route("/healthz")
    def healthz():
        db.session.execute(text("SELECT 1"))
        return {"status": "ok"}, 200

    # --- lightweight page-view counter (no cookies, no IPs) --------------------
    from .models import PageView

    TRACK_EXCLUDE = ("/admin", "/static", "/healthz", "/webhooks", "/auth")

    @app.after_request
    def track_and_harden(response):
        # security headers on everything
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = CSP

        try:
            if (
                request.method == "GET"
                and response.status_code == 200
                and response.mimetype == "text/html"
                and not any(request.path.startswith(p) for p in TRACK_EXCLUDE)
                and len(request.path) <= 300
            ):
                today = date.today()
                row = PageView.query.filter_by(path=request.path, date=today).first()
                if row is None:
                    db.session.add(PageView(path=request.path, date=today, count=1))
                else:
                    row.count += 1
                db.session.commit()
        except Exception:
            db.session.rollback()
        return response

    # --- error pages -----------------------------------------------------------
    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.errorhandler(429)
    def too_many(_e):
        return render_template("errors/429.html"), 429

    return app
