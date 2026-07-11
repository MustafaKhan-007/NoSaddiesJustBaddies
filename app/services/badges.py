"""Achievement badges.

Each *category* has a signature emblem + colour and a ladder of *tiers*
(milestones). The same emblem is reused across a category's tiers, but the SVG
frame grows more ornate (rays -> wings -> ribbon -> gems) and the metal richer
the higher the tier — so "25 posts" and "50 posts" look related but the latter
is clearly more evolved.

A member's badges are derived from their live stats (streak, posts, likes
received), so they can never fall out of sync. Milestone *thresholds* are
editable by the owner in the studio (stored in `Setting["_badge_thresholds"]`);
everything else (emblem, titles, count of tiers) is fixed here.
"""
import json

from sqlalchemy import func

from ..extensions import db
from ..models import (ForumComment, ForumCommentLike, ForumPost, Setting)

_OVERRIDE_KEY = "_badge_thresholds"


def _days(n):
    return f"a {n}-day streak"


def _posts(n):
    return f"{n} post" if n == 1 else f"{n} posts"


def _likes(n):
    return f"{n} like on your comments" if n == 1 else f"{n} likes on your comments"


# --- category definitions ----------------------------------------------------
# metal keys map to gradients defined in templates/partials/badge_defs.html
# tiers are (default_threshold, title); the human phrase is built from the
# live threshold via `phrase` so it stays correct when the owner tweaks values.
CATEGORIES = {
    "showing_up": {
        "name": "Showing Up",
        "emblem": "sunrise",
        "metal": "amber",
        "blurb": "Daily check-ins \u2014 the quiet discipline of returning.",
        "metric_label": "longest streak (days)",
        "phrase": _days,
        "tiers": [
            (3,   "First Light"),
            (7,   "Steady"),
            (30,  "Devoted"),
            (100, "Unbreakable"),
            (365, "A Whole Year"),
        ],
    },
    "storyteller": {
        "name": "Storyteller",
        "emblem": "quill",
        "metal": "plum",
        "blurb": "Posts shared with the community.",
        "metric_label": "posts written",
        "phrase": _posts,
        "tiers": [
            (1,   "First Words"),
            (10,  "Finding Your Voice"),
            (25,  "Storyteller"),
            (50,  "Wordsmith"),
            (100, "Luminary"),
        ],
    },
    "kindred": {
        "name": "Kindred Spirit",
        "emblem": "hearts",
        "metal": "rose",
        "blurb": "Likes earned on your comments \u2014 helpfulness, felt.",
        "metric_label": "likes earned on comments",
        "phrase": _likes,
        "tiers": [
            (5,   "Kind Soul"),
            (25,  "Helper"),
            (50,  "Pillar"),
            (100, "Beloved"),
        ],
    },
}

OWNER_BADGE = {
    "cat": "owner",
    "name": "Founder",
    "emblem": "crown",
    "metal": "owner",
    "level": 5,          # rendered fully ornate — the grandest badge on the site
    "max_level": 5,
    "title": "The Founder",
    "phrase": "she built this place",
    "tooltip": "Founder \u2014 she built this place",
}


# --- thresholds (owner-editable) ---------------------------------------------
def _overrides() -> dict:
    row = db.session.get(Setting, _OVERRIDE_KEY)
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def default_thresholds(cat_key: str) -> list:
    return [t[0] for t in CATEGORIES[cat_key]["tiers"]]


def thresholds(cat_key: str) -> list:
    """Live thresholds for a category (owner override, or the defaults)."""
    defaults = default_thresholds(cat_key)
    ov = _overrides().get(cat_key)
    if not isinstance(ov, list) or len(ov) != len(defaults):
        return defaults
    out = []
    for v in ov:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            return defaults
    return out


def set_thresholds(mapping: dict) -> None:
    """Persist a {cat_key: [int, ...]} override map (validated by the caller)."""
    row = db.session.get(Setting, _OVERRIDE_KEY)
    payload = json.dumps(mapping)
    if row is None:
        db.session.add(Setting(key=_OVERRIDE_KEY, value=payload))
    else:
        row.value = payload
    db.session.commit()


def reset_thresholds() -> None:
    row = db.session.get(Setting, _OVERRIDE_KEY)
    if row is not None:
        db.session.delete(row)
        db.session.commit()


# --- metrics -----------------------------------------------------------------
def _metric(cat_key: str, user) -> int:
    if cat_key == "showing_up":
        return user.longest_streak or 0
    if cat_key == "storyteller":
        return db.session.query(func.count(ForumPost.id)).filter(
            ForumPost.user_id == user.id, ForumPost.hidden.is_(False)).scalar() or 0
    if cat_key == "kindred":
        return db.session.query(func.count(ForumCommentLike.id)).join(
            ForumComment, ForumCommentLike.comment_id == ForumComment.id).filter(
            ForumComment.user_id == user.id).scalar() or 0
    return 0


def _current_level(cat_key: str, metric: int) -> int:
    """1-based index of the highest tier reached, or 0 if none."""
    level = 0
    for i, threshold in enumerate(thresholds(cat_key), start=1):
        if metric >= threshold:
            level = i
    return level


def badge_dict(cat_key: str, level: int) -> dict:
    """Build the render/tooltip payload for a category badge at a given tier."""
    cat = CATEGORIES[cat_key]
    threshold = thresholds(cat_key)[level - 1]
    title = cat["tiers"][level - 1][1]
    phrase = cat["phrase"](threshold)
    return {
        "cat": cat_key,
        "name": cat["name"],
        "emblem": cat["emblem"],
        "metal": cat["metal"],
        "level": level,
        "max_level": len(cat["tiers"]),
        "title": title,
        "threshold": threshold,
        "phrase": phrase,
        "tooltip": f"{cat['name']} \u2014 {title} ({phrase})",
    }


def earned_badges(user) -> list:
    """The member's current (highest) badge in every category they've unlocked."""
    out = []
    for cat_key in CATEGORIES:
        level = _current_level(cat_key, _metric(cat_key, user))
        if level:
            out.append(badge_dict(cat_key, level))
    return out


def category_progress(user) -> list:
    """Every category with the member's current badge (or None) + next milestone.

    Powers the settings "collection" view so members see what they've evolved
    and what's next.
    """
    rows = []
    for cat_key, cat in CATEGORIES.items():
        metric = _metric(cat_key, user)
        level = _current_level(cat_key, metric)
        badge = badge_dict(cat_key, level) if level else None
        next_phrase = None
        ths = thresholds(cat_key)
        if level < len(ths):
            next_phrase = cat["phrase"](ths[level])
        rows.append({
            "cat": cat_key, "name": cat["name"], "blurb": cat["blurb"],
            "metric": metric, "badge": badge, "level": level,
            "preview": badge_dict(cat_key, max(level, 1)),
            "next_phrase": next_phrase,
        })
    return rows


def all_badges_overview() -> list:
    """Every category's full tier ladder — for the studio's badge manager."""
    overview = []
    for cat_key, cat in CATEGORIES.items():
        ths = thresholds(cat_key)
        tiers = []
        for i, (_default, title) in enumerate(cat["tiers"], start=1):
            tiers.append({
                "level": i,
                "title": title,
                "threshold": ths[i - 1],
                "badge": badge_dict(cat_key, i),
            })
        overview.append({
            "cat": cat_key, "name": cat["name"], "blurb": cat["blurb"],
            "metric_label": cat["metric_label"], "tiers": tiers,
            "is_default": ths == default_thresholds(cat_key),
        })
    return overview


def displayed_badges(user) -> list:
    """The up-to-3 category badges the member chose to feature (validated)."""
    out = []
    for cat_key in user.displayed_badges():
        if cat_key not in CATEGORIES:
            continue
        level = _current_level(cat_key, _metric(cat_key, user))
        if level:
            out.append(badge_dict(cat_key, level))
        if len(out) >= 3:
            break
    return out


def profile_badges(user) -> list:
    """What shows on a profile: the owner badge (if any) + chosen/earned badges."""
    badges = []
    if user.is_admin:
        badges.append(OWNER_BADGE)
    chosen = displayed_badges(user)
    if not chosen:
        chosen = _top_earned(user, limit=3)
    badges.extend(chosen)
    return badges[:4]


def primary_badge(user) -> dict | None:
    """The single badge shown next to a member's name in the forums."""
    if user is None:
        return None
    if user.is_admin:
        return OWNER_BADGE
    chosen = displayed_badges(user)
    if chosen:
        return chosen[0]
    top = _top_earned(user, limit=1)
    return top[0] if top else None


def _top_earned(user, limit: int = 3) -> list:
    """Highest-threshold earned badges across categories (fallback ordering)."""
    earned = earned_badges(user)
    earned.sort(key=lambda b: b["threshold"], reverse=True)
    return earned[:limit]
