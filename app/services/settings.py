"""Key-value site settings with a tiny in-process cache."""
import secrets
from datetime import date

from ..extensions import db
from ..models import Setting

#: internal settings (prefixed "_") are never exposed to templates via `site`
SECRET_KEY_SETTING = "_secret_key"

DEFAULTS = {
    "site_title": "Bloom Anyway",
    "instagram_url": "https://instagram.com/",
    "hero_image_url": "",
    "portrait_url": "",
    "contact_email": "",
    "announcement_text": "",
    "announcement_expires": "",   # ISO date (YYYY-MM-DD); blank = never expires
    # home-page spotlight
    "creator_name": "",
    "creator_instagram": "",
    "creator_image_url": "",
    "creator_blurb": "",
    "reel_url": "",
    "reel_description": "",
}

_cache: dict[str, str] = {}
_loaded = False


def _load():
    global _loaded
    _cache.clear()
    for row in Setting.query.all():
        if row.key.startswith("_"):   # internal (e.g. the secret key) — keep private
            continue
        _cache[row.key] = row.value
    _loaded = True


def get_or_create_secret_key() -> str:
    """A stable Flask secret key stored in the database, generated on first use.

    Lets the app run without a SECRET_KEY env var while still surviving restarts.
    """
    row = db.session.get(Setting, SECRET_KEY_SETTING)
    if row is None:
        row = Setting(key=SECRET_KEY_SETTING, value=secrets.token_hex(32))
        db.session.add(row)
        db.session.commit()
    return row.value


def get_setting(key: str, default: str | None = None) -> str:
    if not _loaded:
        _load()
    if default is None:
        default = DEFAULTS.get(key, "")
    return _cache.get(key, default)


def all_settings() -> dict:
    if not _loaded:
        _load()
    merged = dict(DEFAULTS)
    merged.update(_cache)
    return merged


def set_setting(key: str, value: str) -> None:
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()
    _cache[key] = value


def active_announcement() -> str:
    """The announcement text, or "" if unset or past its expiry date."""
    text = get_setting("announcement_text")
    if not text:
        return ""
    expires = get_setting("announcement_expires")
    if expires:
        try:
            if date.fromisoformat(expires) < date.today():
                return ""
        except ValueError:
            pass
    return text


def active_announcements() -> list[str]:
    """All live announcements (multi + the legacy single), newest first."""
    from ..models import Announcement
    out = []
    legacy = active_announcement()
    if legacy:
        out.append(legacy)
    rows = (Announcement.query
            .order_by(Announcement.sort_order, Announcement.created_at.desc()).all())
    out.extend(a.body for a in rows if a.is_live())
    return out


def invalidate_cache() -> None:
    global _loaded
    _loaded = False
