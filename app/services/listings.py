"""Marketplace helpers: image processing + membership-tier limits.

Listing images are re-encoded to safe JPEGs and stored in the database (like
avatars) so they survive deploys. Healing members may run one active listing;
Creator members are unlimited; free/none run none.
"""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from ..extensions import db
from ..models import MARKETPLACE_LIMITS, ListingImage, MarketplaceListing

MAX_UPLOAD_BYTES = 6 * 1024 * 1024
MAX_W, MAX_H = 1200, 900
OUTPUT_MIME = "image/jpeg"
MAX_IMAGES = 5


class ListingError(ValueError):
    pass


def process_listing_image(file_storage) -> tuple[bytes, str]:
    """Return (jpeg_bytes, mime) for an uploaded image, or raise ListingError."""
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise ListingError("One of those images was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ListingError("Each image must be under 6 MB.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        raise ListingError("One of those files wasn't an image we could read.")
    img.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=84, optimize=True)
    return out.getvalue(), OUTPUT_MIME


def listing_limit(user) -> int | None:
    """Max active listings for a user's tier (None = unlimited)."""
    if getattr(user, "is_admin", False):
        return None
    return MARKETPLACE_LIMITS.get(getattr(user, "membership", "none"), 0)


def active_listing_count(user) -> int:
    return MarketplaceListing.query.filter_by(user_id=user.id, active=True).count()


def can_add_listing(user) -> bool:
    limit = listing_limit(user)
    if limit is None:
        return True
    return active_listing_count(user) < limit


def enforce_listing_limits(user) -> None:
    """Deactivate listings that exceed the user's current tier allowance (used
    when a membership is cancelled or downgraded). Keeps the newest ones."""
    limit = listing_limit(user)
    if limit is None:
        return
    actives = (MarketplaceListing.query
               .filter_by(user_id=user.id, active=True)
               .order_by(MarketplaceListing.created_at.desc()).all())
    for extra in actives[limit:]:
        extra.active = False
    db.session.add(user)
