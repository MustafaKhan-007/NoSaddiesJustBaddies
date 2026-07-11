"""Achievement badges.

Each *category* has a signature emblem + colour and a ladder of *tiers*
(milestones). The same emblem is reused across a category's tiers, but the SVG
frame grows more ornate (rays -> wings -> ribbon -> gems) and the metal richer
the higher the tier — so "25 posts" and "50 posts" look related but the latter
is clearly more evolved.

Nothing here stores badge state: a member's badges are derived from their live
stats (streak, posts, likes received), so they can never fall out of sync.
"""
from datetime import date

from sqlalchemy import func

from ..extensions import db
from ..models import (ForumComment, ForumCommentLike, ForumPost, User)

# --- category definitions ----------------------------------------------------
# metal keys map to gradients defined in templates/partials/badge_defs.html
CATEGORIES = {
    "showing_up": {
        "name": "Showing Up",
        "emblem": "sunrise",
        "metal": "amber",
        "blurb": "Daily check-ins \u2014 the quiet discipline of returning.",
        # (threshold, tier title, human phrase for the tooltip)
        "tiers": [
            (3,   "First Light",   "a 3-day streak"),
            (7,   "Steady",        "a 7-day streak"),
            (30,  "Devoted",       "a 30-day streak"),
            (100, "Unbreakable",   "a 100-day streak"),
            (365, "A Whole Year",  "a 365-day streak"),
        ],
    },
    "storyteller": {
        "name": "Storyteller",
        "emblem": "quill",
        "metal": "plum",
        "blurb": "Posts shared with the community.",
        "tiers": [
            (1,   "First Words",       "1 post"),
            (10,  "Finding Your Voice","10 posts"),
            (25,  "Storyteller",       "25 posts"),
            (50,  "Wordsmith",         "50 posts"),
            (100, "Luminary",          "100 posts"),
        ],
    },
    "kindred": {
        "name": "Kindred Spirit",
        "emblem": "hearts",
        "metal": "rose",
        "blurb": "Likes earned on your comments \u2014 helpfulness, felt.",
        "tiers": [
            (5,   "Kind Soul", "5 likes on your comments"),
            (25,  "Helper",    "25 likes on your comments"),
            (50,  "Pillar",    "50 likes on your comments"),
            (100, "Beloved",   "100 likes on your comments"),
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
    tiers = CATEGORIES[cat_key]["tiers"]
    level = 0
    for i, (threshold, _title, _phrase) in enumerate(tiers, start=1):
        if metric >= threshold:
            level = i
    return level


def badge_dict(cat_key: str, level: int) -> dict:
    """Build the render/tooltip payload for a category badge at a given tier."""
    cat = CATEGORIES[cat_key]
    threshold, title, phrase = cat["tiers"][level - 1]
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
        if level < len(cat["tiers"]):
            next_threshold, _t, next_phrase = cat["tiers"][level]
        rows.append({
            "cat": cat_key, "name": cat["name"], "blurb": cat["blurb"],
            "metric": metric, "badge": badge, "level": level,
            "preview": badge_dict(cat_key, max(level, 1)),
            "next_phrase": next_phrase,
        })
    return rows


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
