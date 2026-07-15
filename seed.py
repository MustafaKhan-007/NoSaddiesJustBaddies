"""Idempotent seed script — content only, never credentials.

- Loads data/quotes_seed.json, skipping quotes whose text already exists
  (case-insensitive).
- Creates starter FAQ items and legal page stubs if none exist.

The owner/admin account is created in the browser at /setup (one-time page,
locks itself after the owner's first sign-in). Passwords are never written
by this script, so password changes always survive redeploys.

Run after `flask db upgrade`:  python seed.py
"""
import json
from pathlib import Path

from app import create_app
from app.extensions import db
from app.models import FaqItem, ForumCategory, ForumTag, Page, Product, Quote

SEED_FILE = Path(__file__).parent / "data" / "quotes_seed.json"

# Two forums, each with topic tags used for reader filters and author labels.
FORUMS = [
    {"slug": "building", "name": "Building", "accent": "#f0a202", "sort": 0,
     "description": "Growth, goals, and the brave work of creating a life and a craft.",
     "tags": [("content", "Content & Creating"), ("starting-over", "Starting Over"),
              ("work-money", "Work & Money"), ("wins", "Small Wins")]},
    {"slug": "healing", "name": "Healing", "accent": "#7b6cf6", "sort": 1,
     "description": "Room to process, grieve, vent, and find your footing again.",
     "tags": [("venting", "The Vent"), ("divorce-custody", "Divorce & Custody"),
              ("grief", "Grief & Loss"), ("confidence", "Confidence")]},
]
# categories seeded by the previous version, now folded into tags above
RETIRED_CATEGORY_SLUGS = ["venting", "divorce-custody", "content-creation",
                          "starting-over", "wins"]

STARTER_FAQS = [
    ("How do I get my files after buying?",
     "The moment your payment goes through, Lemon Squeezy emails you a receipt "
     "with your download links. Check spam if it's shy. You can always re-send "
     "them from [your orders page](https://app.lemonsqueezy.com/my-orders).", 0),
    ("Do I need an account here to buy?",
     "No. Checkout works without one. An account just adds saved quotes, the "
     "community forums, and course picks made for you \u2014 it's free.", 1),
    ("What's your refund policy?",
     "See the [refund policy](/refunds) page. Short version: I'd rather you be "
     "honest with me than stuck with something that isn't helping.", 2),
    ("Is this therapy?",
     "No \u2014 and it doesn't pretend to be. These are practical courses and "
     "notebooks. If you're in crisis, please reach out to a professional or a "
     "local helpline first. This will be here after.", 3),
]

# Membership products, created as drafts. Add a Lemon Squeezy buy link + cover
# and publish them to start selling. Buying one auto-upgrades the buyer's tier.
MEMBERSHIP_PRODUCTS = [
    {"slug": "healing-membership", "title": "Healing Membership", "grants": "healing",
     "promise": "Full community access \u2014 post, reply and read every thread.",
     "price_cents": 900,
     "description": "A place to process, vent and be met with kindness. Healing "
                    "members can post, reply and like across both community forums, "
                    "with no peeking limits.",
     "sort": 90},
    {"slug": "creator-membership", "title": "Creator Membership", "grants": "creator",
     "promise": "Everything in Healing, plus the video room, profile links, the "
                "spotlight and the My Journey keepsake.",
     "price_cents": 1900,
     "description": "For the ones building in public. Creator members get the full "
                    "community, the owner's video room, social links on their profile, "
                    "a shot at the home-page spotlight, and the My Journey PDF export.",
     "sort": 91},
]

LEGAL_STUBS = {
    "privacy": ("Privacy Policy",
                "*TODO: legal review.*\n\nWe collect the minimum needed to run this site: "
                "your email if you create an account or join the letter, and order records "
                "delivered by our payment provider (Lemon Squeezy), who is the merchant of "
                "record. We never see or store card details. No tracking cookies, no ad pixels."),
    "terms": ("Terms of Service",
              "*TODO: legal review.*\n\nDigital products are licensed for personal use. "
              "Payments, taxes and delivery are handled by Lemon Squeezy as merchant of record."),
    "refunds": ("Refund Policy",
                "*TODO: legal review.*\n\nIf something isn't working for you, reply to your "
                "receipt email within 14 days and we'll make it right."),
}


def seed():
    app = create_app()
    with app.app_context():
        # 1. quotes (idempotent on lowercase text)
        payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        existing = {q.text.strip().lower() for q in Quote.query.all()}
        added = 0
        for row in payload["quotes"]:
            key = row["text"].strip().lower()
            if key in existing:
                continue
            db.session.add(Quote(text=row["text"], author=row.get("author"),
                                 category=row["category"], active=True))
            existing.add(key)
            added += 1
        print(f"Quotes: added {added}, skipped {len(payload['quotes']) - added} existing")

        # 2. starter FAQ
        if FaqItem.query.count() == 0:
            for question, answer, order in STARTER_FAQS:
                db.session.add(FaqItem(question=question, answer_md=answer, sort_order=order))
            print(f"Added {len(STARTER_FAQS)} starter FAQ items")

        # 3. legal page stubs
        for slug, (title, body) in LEGAL_STUBS.items():
            if Page.query.filter_by(slug=slug).first() is None:
                db.session.add(Page(slug=slug, title=title, body_md=body))
                print(f"Created page stub: {slug}")

        # 4. forums + topic tags (idempotent). Retire the old single-topic
        #    categories once they're empty — they live on as tags now.
        removed = 0
        for slug in RETIRED_CATEGORY_SLUGS:
            old = ForumCategory.query.filter_by(slug=slug).first()
            if old and old.posts.count() == 0:
                db.session.delete(old)
                removed += 1
        if removed:
            print(f"Retired {removed} old forum categories")

        cat_added = tag_added = 0
        for f in FORUMS:
            cat = ForumCategory.query.filter_by(slug=f["slug"]).first()
            if cat is None:
                cat = ForumCategory(slug=f["slug"])
                db.session.add(cat)
                cat_added += 1
            cat.name = f["name"]
            cat.description = f["description"]
            cat.accent = f["accent"]
            cat.sort_order = f["sort"]
            db.session.flush()
            for order, (tslug, tname) in enumerate(f["tags"]):
                if ForumTag.query.filter_by(category_id=cat.id, slug=tslug).first() is None:
                    db.session.add(ForumTag(category_id=cat.id, slug=tslug,
                                            name=tname, sort_order=order))
                    tag_added += 1
        if cat_added or tag_added:
            print(f"Forums: added {cat_added} categories, {tag_added} tags")

        # 5. membership products (drafts; owner adds a buy link + publishes)
        mem_added = 0
        for m in MEMBERSHIP_PRODUCTS:
            if Product.query.filter_by(slug=m["slug"]).first() is None:
                db.session.add(Product(
                    slug=m["slug"], title=m["title"], type="course",
                    grants_membership=m["grants"], status="draft",
                    promise=m["promise"], description_md=m["description"],
                    price_cents=m["price_cents"], sort_order=m["sort"]))
                mem_added += 1
        if mem_added:
            print(f"Added {mem_added} membership product drafts")

        db.session.commit()
        print("Seed complete.")


if __name__ == "__main__":
    seed()
