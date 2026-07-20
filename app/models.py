"""All SQLAlchemy models."""
import json
from datetime import date, datetime, timedelta, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

#: number of profanity warnings allowed before a forum ban (the ban lands on
#: the next offense after this many warnings)
FORUM_WARNING_LIMIT = 2


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- constants (kept as plain strings for SQLite/Postgres portability) ------
PRODUCT_TYPES = ("course", "guide")
PRODUCT_STATUSES = ("draft", "published", "archived")
QUOTE_CATEGORIES = ("comfort", "determination", "renewal")

#: membership tiers. "none" = free (limited forum peek + shop); "healing" =
#: full community read + post; "creator" = healing perks + videos, My Journey
#: export, profile links, and eligibility for the home-page spotlight.
MEMBERSHIPS = ("none", "healing", "creator")
MEMBERSHIP_LABELS = {"none": "Free", "healing": "Healing", "creator": "Creator"}
#: ordering so we can compare / take the "highest" tier a member holds
MEMBERSHIP_RANK = {"none": 0, "healing": 1, "creator": 2}


def higher_membership(a: str, b: str) -> str:
    """Return whichever of two tiers ranks higher."""
    a, b = a or "none", b or "none"
    return a if MEMBERSHIP_RANK.get(a, 0) >= MEMBERSHIP_RANK.get(b, 0) else b

#: subjects a course/guide can be filed under (owner picks one; drives the
#: filter tabs on the catalogue).
PRODUCT_SUBJECTS = (
    "Healing", "Confidence", "Relationships", "Parenting", "Money",
    "Creativity", "Content Creation", "Productivity", "Mindfulness", "Career",
)

#: how many free (no-membership) visitors can peek at in the community
FREE_POSTS_PER_CATEGORY = 3
FREE_COMMENTS_PER_POST = 5


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255))
    email_verified_at = db.Column(db.DateTime)
    display_name = db.Column(db.String(80))
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)

    # profile / personalization
    avatar_url = db.Column(db.String(500))   # legacy external URL (still shown if set)
    avatar_data = db.Column(db.LargeBinary)  # uploaded avatar bytes (survives deploys)
    avatar_mime = db.Column(db.String(40))
    bio = db.Column(db.String(400))
    links_json = db.Column(db.Text)          # JSON list of {"label","url"}
    goals_json = db.Column(db.Text)          # JSON list of intent keys
    default_anonymous = db.Column(db.Boolean, nullable=False, default=False)

    # forum moderation
    forum_warnings = db.Column(db.Integer, nullable=False, default=0)
    forum_banned = db.Column(db.Boolean, nullable=False, default=False)

    # membership tier: none / healing / creator (owner-assigned)
    membership = db.Column(db.String(20), nullable=False, default="none")

    # showing-up streak ("I showed up today")
    last_checkin_date = db.Column(db.Date)
    current_streak = db.Column(db.Integer, nullable=False, default=0)
    longest_streak = db.Column(db.Integer, nullable=False, default=0)
    total_checkins = db.Column(db.Integer, nullable=False, default=0)

    # up to 3 badge category keys the member chose to feature on their profile
    displayed_badges_json = db.Column(db.Text)

    codes = db.relationship("VerificationCode", backref="user", lazy="dynamic",
                            cascade="all, delete-orphan")
    favorites = db.relationship("QuoteFavorite", backref="user", lazy="dynamic",
                                cascade="all, delete-orphan")

    @property
    def is_active(self):  # Flask-Login: soft-deleted users cannot log in
        return self.deleted_at is None

    @property
    def is_verified(self):
        return self.email_verified_at is not None

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def first_name(self):
        if self.display_name:
            return self.display_name.split()[0]
        return None

    def public_name(self):
        return self.display_name or "Member"

    def initials(self):
        base = (self.display_name or self.email or "?").strip()
        parts = base.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return base[0].upper()

    def has_avatar(self) -> bool:
        return self.avatar_data is not None

    # --- membership tiers ---------------------------------------------------
    def effective_membership(self) -> str:
        """The tier used for gating. Owner always ranks as Creator — even if
        the stored column is still ``none`` from before memberships existed."""
        if self.is_admin:
            return "creator"
        return self.membership or "none"

    def is_creator(self) -> bool:
        """Creator tier (or owner): all perks."""
        return self.effective_membership() == "creator"

    def is_member(self) -> bool:
        """Healing or Creator (or owner): full community access."""
        return self.effective_membership() in ("healing", "creator")

    def membership_label(self) -> str:
        if self.is_admin:
            return "Owner"
        return MEMBERSHIP_LABELS.get(self.effective_membership(), "Free")

    def goals(self) -> list:
        try:
            return json.loads(self.goals_json) if self.goals_json else []
        except ValueError:
            return []

    def set_goals(self, keys) -> None:
        self.goals_json = json.dumps(list(keys)) if keys else None

    def links(self) -> list:
        try:
            return json.loads(self.links_json) if self.links_json else []
        except ValueError:
            return []

    def set_links(self, links) -> None:
        self.links_json = json.dumps(list(links)) if links else None

    def displayed_badges(self) -> list:
        try:
            return json.loads(self.displayed_badges_json) if self.displayed_badges_json else []
        except ValueError:
            return []

    def set_displayed_badges(self, keys) -> None:
        self.displayed_badges_json = json.dumps(list(keys)[:3]) if keys else None

    def check_in(self) -> bool:
        """Record 'I showed up today'. Returns True if this was a new check-in."""
        today = date.today()
        if self.last_checkin_date == today:
            return False
        if self.last_checkin_date == today - timedelta(days=1):
            self.current_streak = (self.current_streak or 0) + 1
        else:
            self.current_streak = 1
        self.last_checkin_date = today
        self.total_checkins = (self.total_checkins or 0) + 1
        self.longest_streak = max(self.longest_streak or 0, self.current_streak)
        # per-day log so a "My Journey" export can show real history
        db.session.add(CheckIn(user_id=self.id, day=today))
        return True

    def checked_in_today(self) -> bool:
        return self.last_checkin_date == date.today()

    def streak_display(self) -> int:
        """Current streak, but shown as 0 if it lapsed (missed yesterday+today)."""
        if self.last_checkin_date is None:
            return 0
        if self.last_checkin_date >= date.today() - timedelta(days=1):
            return self.current_streak or 0
        return 0


class VerificationCode(db.Model):
    """One-time 6-digit email codes (account confirmation / password reset).

    Only the SHA-256 hash of the code is stored. Codes are single-use,
    expire after 15 minutes, and allow at most 5 wrong attempts.
    """
    __tablename__ = "verification_codes"

    PURPOSES = ("confirm", "reset")
    MAX_ATTEMPTS = 5

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    code_hash = db.Column(db.String(64), nullable=False)
    purpose = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    request_ip = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def is_usable(self) -> bool:
        return (self.used_at is None
                and self.expires_at > utcnow()
                and self.attempts < self.MAX_ATTEMPTS)


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(160), unique=True, nullable=False)
    type = db.Column(db.String(20), nullable=False, default="course")
    subject = db.Column(db.String(60))   # filterable catalogue subject
    status = db.Column(db.String(20), nullable=False, default="draft")
    featured = db.Column(db.Boolean, nullable=False, default=False)
    badge = db.Column(db.String(30))
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    promise = db.Column(db.String(120))
    description_md = db.Column(db.Text)
    audience = db.Column(db.Text)          # "Who this is for"
    contents_text = db.Column(db.Text)     # one item per line -> check-list
    curriculum_json = db.Column(db.Text)   # JSON: [{title, description}]

    cover_url = db.Column(db.String(500))
    gallery_json = db.Column(db.Text)      # JSON: [url, ...]

    price_cents = db.Column(db.Integer)
    compare_at_cents = db.Column(db.Integer)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    ls_checkout_url = db.Column(db.String(500))
    ls_variant_id = db.Column(db.String(40), index=True)

    meta_title = db.Column(db.String(160))
    meta_description = db.Column(db.String(200))

    # hidden recommendation tags (never shown to customers)
    tags_json = db.Column(db.Text)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    orders = db.relationship("Order", backref="product", lazy="dynamic")
    testimonials = db.relationship("Testimonial", backref="product", lazy="dynamic")
    assets = db.relationship("ProductAsset", backref="product", lazy="select",
                             order_by="ProductAsset.sort_order, ProductAsset.id",
                             cascade="all, delete-orphan")

    def has_assets(self) -> bool:
        return len(self.assets) > 0

    def tags(self) -> list:
        try:
            return json.loads(self.tags_json) if self.tags_json else []
        except ValueError:
            return []

    def set_tags(self, tags) -> None:
        cleaned = [t.strip().lower() for t in tags if t.strip()]
        self.tags_json = json.dumps(cleaned) if cleaned else None

    def price_display(self):
        if self.price_cents is None:
            return ""
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        amount = self.price_cents / 100
        return f"{symbol}{amount:,.0f}" if self.price_cents % 100 == 0 else f"{symbol}{amount:,.2f}"

    def compare_at_display(self):
        if self.compare_at_cents is None:
            return ""
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        amount = self.compare_at_cents / 100
        return f"{symbol}{amount:,.0f}" if self.compare_at_cents % 100 == 0 else f"{symbol}{amount:,.2f}"

    def type_label(self):
        return "Course" if self.type == "course" else "Notebook Guide"

    def publish_blockers(self):
        """List of human-readable requirements missing before publishing."""
        missing = []
        if not (self.promise or "").strip():
            missing.append("a one-line promise")
        if not (self.cover_url or "").strip():
            missing.append("a cover image URL")
        if self.price_cents is None:
            missing.append("a price")
        if not (self.ls_checkout_url or "").strip():
            missing.append("the Lemon Squeezy buy link")
        # Without the variant ID, webhooks can't put the purchase into My space.
        if not (self.ls_variant_id or "").strip():
            missing.append("the Lemon Squeezy variant ID")
        return missing


class ProductAsset(db.Model):
    """Course/guide file for on-site reading (not a public download).

    Stored in the database (like avatars) so files survive Render's ephemeral
    disk. Served inline to buyers in My space via an ownership-gated route.
    """
    __tablename__ = "product_assets"

    KINDS = ("pdf", "doc", "docx")

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    title = db.Column(db.String(160))
    filename = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(10), nullable=False)   # pdf / doc / docx
    size = db.Column(db.Integer, nullable=False, default=0)
    data = db.Column(db.LargeBinary, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def display_title(self):
        return self.title or self.filename

    def size_mb(self):
        return round((self.size or 0) / 1024 / 1024, 1)


class MembershipPlan(db.Model):
    """A sellable membership (Healing / Creator). Sold on its own — not a
    product/course. Buying one (matched by Lemon Squeezy variant id on the
    order) upgrades the buyer's `users.membership` tier."""
    __tablename__ = "membership_plans"

    id = db.Column(db.Integer, primary_key=True)
    tier = db.Column(db.String(20), unique=True, nullable=False)  # healing / creator
    name = db.Column(db.String(80), nullable=False)
    tagline = db.Column(db.String(160))
    price_cents = db.Column(db.Integer)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    period = db.Column(db.String(20), nullable=False, default="month")  # month / year / once
    ls_variant_id = db.Column(db.String(40), index=True)
    ls_checkout_url = db.Column(db.String(500))
    active = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    def label(self):
        return MEMBERSHIP_LABELS.get(self.tier, self.tier.title())

    def price_display(self):
        if self.price_cents is None:
            return ""
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        amount = self.price_cents / 100
        return f"{symbol}{amount:,.0f}" if self.price_cents % 100 == 0 else f"{symbol}{amount:,.2f}"

    def period_label(self):
        return {"month": "/ month", "year": "/ year", "once": "one-time"}.get(self.period, "")

    def is_buyable(self):
        return bool(self.active and self.ls_checkout_url)


class Quote(db.Model):
    __tablename__ = "quotes"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(240), nullable=False)
    author = db.Column(db.String(120))
    category = db.Column(db.String(20), nullable=False, default="comfort")
    active = db.Column(db.Boolean, nullable=False, default=True)
    times_shown = db.Column(db.Integer, nullable=False, default=0)
    last_shown_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    favorites = db.relationship("QuoteFavorite", backref="quote", lazy="dynamic",
                                cascade="all, delete-orphan")


class QuotePin(db.Model):
    __tablename__ = "quote_pins"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False)

    quote = db.relationship("Quote")


class QuoteFavorite(db.Model):
    __tablename__ = "quote_favorites"
    __table_args__ = (db.UniqueConstraint("user_id", "quote_id", name="uq_favorite_user_quote"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class CheckIn(db.Model):
    """One row per day a member 'shows up' — the raw history behind streaks."""
    __tablename__ = "check_ins"
    __table_args__ = (db.UniqueConstraint("user_id", "day", name="uq_checkin_user_day"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Video(db.Model):
    """An owner-uploaded video (Creator-membership perk). The file is streamed
    to a directory on disk (a mounted persistent disk in production) so large
    uploads don't exhaust worker memory; only the small thumbnail lives in the
    DB. ``disk_name`` is the file's name within VIDEO_STORAGE_DIR."""
    __tablename__ = "videos"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    filename = db.Column(db.String(255))   # original upload name (for download)
    disk_name = db.Column(db.String(64))   # stored file name on disk
    mime = db.Column(db.String(120), nullable=False)
    size = db.Column(db.Integer, nullable=False, default=0)
    data = db.Column(db.LargeBinary)       # legacy DB-stored bytes (older rows)
    thumb_data = db.Column(db.LargeBinary)
    thumb_mime = db.Column(db.String(40))
    published = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def has_thumb(self) -> bool:
        return self.thumb_data is not None

    def size_mb(self):
        return round((self.size or 0) / 1024 / 1024, 1)


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    ls_order_id = db.Column(db.String(40), unique=True, nullable=False)
    ls_variant_id = db.Column(db.String(40))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    buyer_email = db.Column(db.String(255), nullable=False, index=True)
    # if the buyer gifted this to a friend, the friend's account email gets
    # access to the product's files instead of/along with the buyer
    gift_to_email = db.Column(db.String(255), index=True)
    total_cents = db.Column(db.Integer, nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    status = db.Column(db.String(20), nullable=False, default="paid")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def total_display(self):
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        return f"{symbol}{self.total_cents / 100:,.2f}"

    def masked_email(self):
        try:
            local, domain = self.buyer_email.split("@", 1)
            return f"{local[0]}\u2022\u2022\u2022@{domain}"
        except ValueError:
            return "\u2022\u2022\u2022"


class Subscriber(db.Model):
    __tablename__ = "subscribers"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Testimonial(db.Model):
    __tablename__ = "testimonials"

    id = db.Column(db.Integer, primary_key=True)
    quote = db.Column(db.Text, nullable=False)
    first_name = db.Column(db.String(60), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    show_on_home = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class FaqItem(db.Model):
    __tablename__ = "faq_items"

    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(240), nullable=False)
    answer_md = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class Page(db.Model):
    __tablename__ = "pages"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body_md = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


class PageView(db.Model):
    __tablename__ = "page_views"
    __table_args__ = (db.UniqueConstraint("path", "date", name="uq_pageview_path_date"),)

    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(300), nullable=False)
    date = db.Column(db.Date, nullable=False)
    count = db.Column(db.Integer, nullable=False, default=0)


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# --- community forums --------------------------------------------------------

class ForumCategory(db.Model):
    __tablename__ = "forum_categories"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(240), nullable=False, default="")
    accent = db.Column(db.String(7))          # optional hex colour for the card
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    posts = db.relationship("ForumPost", backref="category", lazy="dynamic",
                            cascade="all, delete-orphan")
    tags = db.relationship("ForumTag", backref="category", lazy="dynamic",
                           cascade="all, delete-orphan",
                           order_by="ForumTag.sort_order")


class ForumTag(db.Model):
    """A topic label within a forum (e.g. "Divorce & Custody" under Healing)."""
    __tablename__ = "forum_tags"
    __table_args__ = (db.UniqueConstraint("category_id", "slug", name="uq_tag_category_slug"),)

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("forum_categories.id"), nullable=False, index=True)
    slug = db.Column(db.String(60), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class ForumPost(db.Model):
    __tablename__ = "forum_posts"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("forum_categories.id"), nullable=False, index=True)
    tag_id = db.Column(db.Integer, db.ForeignKey("forum_tags.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=False)
    anonymous = db.Column(db.Boolean, nullable=False, default=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    tag = db.relationship("ForumTag")
    comments = db.relationship("ForumComment", backref="post", lazy="dynamic",
                               cascade="all, delete-orphan")
    likes = db.relationship("ForumPostLike", backref="post", lazy="dynamic",
                            cascade="all, delete-orphan")

    def display_author(self):
        return "Anonymous" if self.anonymous else self.author.public_name()

    def like_count(self):
        return self.likes.count()


class ForumComment(db.Model):
    __tablename__ = "forum_comments"

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_posts.id"), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("forum_comments.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    anonymous = db.Column(db.Boolean, nullable=False, default=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    # one level of replies only (a reply cannot itself be replied to)
    replies = db.relationship("ForumComment",
                              backref=db.backref("parent", remote_side=[id]),
                              lazy="select", order_by="ForumComment.created_at",
                              cascade="all, delete-orphan")
    likes = db.relationship("ForumCommentLike", backref="comment", lazy="dynamic",
                            cascade="all, delete-orphan")

    def display_author(self):
        return "Anonymous" if self.anonymous else self.author.public_name()

    def like_count(self):
        return self.likes.count()


class ForumPostLike(db.Model):
    __tablename__ = "forum_post_likes"
    __table_args__ = (db.UniqueConstraint("user_id", "post_id", name="uq_postlike_user_post"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_posts.id"), nullable=False)


class ForumCommentLike(db.Model):
    __tablename__ = "forum_comment_likes"
    __table_args__ = (db.UniqueConstraint("user_id", "comment_id", name="uq_commentlike_user_comment"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment_id = db.Column(db.Integer, db.ForeignKey("forum_comments.id"), nullable=False)


# --- announcements ----------------------------------------------------------

class Announcement(db.Model):
    """A home-page announcement. Several can be live at once; they stack tidily.
    Non-dismissible; the owner sets an optional expiry."""
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.String(300), nullable=False)
    expires = db.Column(db.Date)   # defaults to +1 day when created from Studio
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def is_live(self) -> bool:
        return self.expires is None or self.expires >= date.today()


# --- marketplace ------------------------------------------------------------

MARKETPLACE_KINDS = ("product", "service")
MARKETPLACE_KIND_LABELS = {"product": "Digital product", "service": "Service"}
#: how many active listings each tier may run at once (creator = unlimited)
MARKETPLACE_LIMITS = {"none": 0, "healing": 1, "creator": None}
#: how many tags a single listing may carry
MARKETPLACE_TAG_MAX = 24
#: curated tag catalogue (authors pick from these; filters use the same list)
MARKETPLACE_TAGS = (
    # healing & growth
    "Healing", "Trauma-informed", "Grief", "Divorce", "Custody", "Co-parenting",
    "Single moms", "Starting over", "Confidence", "Boundaries", "Anxiety",
    "Self-care", "Mindfulness", "Meditation", "Journaling", "Affirmations",
    "Faith", "Spirituality", "Energy healing", "Reiki", "Astrology", "Tarot",
    "LGBTQ+", "BIPOC", "Accountability", "Mentorship", "Coaching", "1:1",
    "Group", "Workshop", "Community",
    # body & home
    "Fitness", "Yoga", "Pilates", "Personal training", "Nutrition", "Meal plans",
    "Recipes", "Cooking", "Skincare", "Beauty", "Makeup", "Fashion", "Hair",
    "Home", "Interior design", "Organizing", "Cleaning", "Pet care", "Childcare",
    # creating & content
    "Content creation", "Instagram", "TikTok", "YouTube", "UGC", "Influencer",
    "Branding", "Canva", "Copywriting", "Ghostwriting", "Writing", "Editing",
    "Photography", "Presets", "Videography", "CapCut", "Premiere", "Podcast",
    "Speaking", "Public speaking",
    # business & money
    "Business", "Freelance", "Side hustle", "Money", "Budgeting", "Career",
    "Resume", "Interview prep", "Marketing", "SEO", "Email marketing",
    "Affiliate", "Etsy", "Shopify", "Amazon", "Dropshipping", "VA",
    "Bookkeeping", "Legal", "Real estate", "Event planning",
    # digital products & formats
    "Course", "Ebook", "Workbook", "Planner", "Printable", "Template",
    "Notion", "Spreadsheet", "Tracker", "Stickers", "Wall art", "Bundle",
    "Freebie", "Subscription", "Membership", "Digital download", "Handmade",
    "Jewelry", "Clothing", "Candles",
    # delivery / format
    "Online", "Remote", "In-person", "Hybrid", "Downloadable", "Live session",
    "Async", "Beginner-friendly", "Advanced",
)


class MarketplaceListing(db.Model):
    """A member-run advert for a digital product or a service. We only
    advertise here and redirect to the seller's own site — no checkout."""
    __tablename__ = "marketplace_listings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default="product")  # product / service
    title = db.Column(db.String(140), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    location = db.Column(db.String(120))     # services only
    price = db.Column(db.String(80))         # free text, e.g. "$49" or "From $20/hr"
    website_url = db.Column(db.String(500), nullable=False)
    tags_json = db.Column(db.Text)           # JSON list of free-form tags
    clicks = db.Column(db.Integer, nullable=False, default=0)  # outbound clicks (popularity)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    images = db.relationship("ListingImage", backref="listing", lazy="select",
                             order_by="ListingImage.sort_order",
                             cascade="all, delete-orphan")

    def kind_label(self):
        return MARKETPLACE_KIND_LABELS.get(self.kind, "Listing")

    def tags(self) -> list:
        try:
            return json.loads(self.tags_json) if self.tags_json else []
        except ValueError:
            return []

    def set_tags(self, tags) -> None:
        self.tags_json = json.dumps(list(tags)) if tags else None

    def thumb(self):
        return self.images[0] if self.images else None


class ListingImage(db.Model):
    __tablename__ = "listing_images"

    id = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(db.Integer, db.ForeignKey("marketplace_listings.id"),
                           nullable=False, index=True)
    data = db.Column(db.LargeBinary, nullable=False)
    mime = db.Column(db.String(40), nullable=False, default="image/jpeg")
    sort_order = db.Column(db.Integer, nullable=False, default=0)


# --- reel reviews (Content Hub) ---------------------------------------------

class ReelReviewApplication(db.Model):
    """A Creator member's weekly request for a reel review.

    One application per user per ISO week (``week_key`` = that Monday). Each
    week one applicant is randomly selected; Monday clears the slate.
    """
    __tablename__ = "reel_review_applications"
    __table_args__ = (
        db.UniqueConstraint("user_id", "week_key", name="uq_reel_app_user_week"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    week_key = db.Column(db.Date, nullable=False, index=True)  # Monday of the week
    reel_url = db.Column(db.String(500), nullable=False)
    disk_name = db.Column(db.String(64))   # raw video on disk
    filename = db.Column(db.String(255))
    mime = db.Column(db.String(120), nullable=False, default="video/mp4")
    size = db.Column(db.Integer, nullable=False, default=0)
    selected = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    review = db.relationship("ReelReview", backref="application", uselist=False,
                             cascade="all, delete-orphan")


class ReelReview(db.Model):
    """A published reel review — public on the Content Hub."""
    __tablename__ = "reel_reviews"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("reel_review_applications.id"),
                               nullable=False, unique=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=False, default="")
    review_disk_name = db.Column(db.String(64))  # optional owner review video
    review_mime = db.Column(db.String(120))
    review_filename = db.Column(db.String(255))
    published = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# --- 1:1 coaching requests --------------------------------------------------

class CoachingRequest(db.Model):
    """A Creator member's request for a $100 1-on-1 coaching session."""
    __tablename__ = "coaching_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    message = db.Column(db.Text, nullable=False, default="")
    preferred_times = db.Column(db.String(300))
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending/booked/done/cancelled
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
