"""Lightweight profanity guard for the community forums.

Not a perfect filter — a small, word-boundary blocklist that keeps the space
kind. Offending content is blocked (never stored); the author collects a
warning, and after the limit the next offense removes their posting access.
"""
import re

from ..extensions import db
from ..models import FORUM_WARNING_LIMIT

#: base word list (matched on word boundaries, case-insensitive). Deliberately
#: small and focused on slurs/expletives rather than trying to catch everything.
_BLOCKLIST = {
    "fuck", "fucking", "fucked", "shit", "shitty", "bitch", "bastard",
    "asshole", "dick", "cunt", "slut", "whore", "faggot", "nigger", "retard",
    "motherfucker", "bullshit", "prick", "wanker", "twat",
}

# catch common leetspeak/spacing dodges: f u c k, sh1t, f*ck, etc.
_LEET = str.maketrans({"1": "i", "3": "e", "0": "o", "4": "a", "5": "s",
                       "@": "a", "$": "s", "!": "i"})

_WORD_RE = re.compile(r"[a-z]+")


def _normalize(text: str) -> str:
    lowered = (text or "").lower().translate(_LEET)
    # collapse punctuation/spacing between single letters (f*u*c*k -> fuck)
    return re.sub(r"[^a-z0-9]+", " ", lowered)


def contains_profanity(text: str) -> bool:
    normalized = _normalize(text)
    words = set(_WORD_RE.findall(normalized))
    if words & _BLOCKLIST:
        return True
    # also catch spaced-out attempts by squashing all whitespace
    squashed = normalized.replace(" ", "")
    return any(bad in squashed for bad in _BLOCKLIST)


def register_violation(user) -> dict:
    """Record a profanity offense against a user. Returns a status dict:

    {"banned": bool, "warnings": int, "remaining": int, "message": str}
    """
    user.forum_warnings = (user.forum_warnings or 0) + 1
    if user.forum_warnings > FORUM_WARNING_LIMIT:
        user.forum_banned = True
    db.session.commit()

    remaining = max(0, FORUM_WARNING_LIMIT - user.forum_warnings + 1)
    if user.forum_banned:
        message = ("That language crossed the line one time too many, so posting "
                   "is now closed for your account. Reach out if you'd like to talk it through.")
    elif remaining <= 0:
        message = ("We keep this space kind, so that didn't post. This is your final "
                   "warning — the next one pauses your posting access.")
    else:
        warn_no = user.forum_warnings
        message = (f"We keep this space kind, so that didn't post. Gentle warning "
                   f"{warn_no} of {FORUM_WARNING_LIMIT} — please keep it clean.")
    return {
        "banned": user.forum_banned,
        "warnings": user.forum_warnings,
        "remaining": remaining,
        "message": message,
    }
