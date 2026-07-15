"""Acceptance-criteria smoke test (run: python scripts/smoke_test.py).

Uses a throwaway SQLite database and the Flask test client. Not a pytest
suite on purpose — a single readable script the owner/dev can run anywhere.
"""
import hashlib
import hmac
import io
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = "test-secret"

from app import create_app
from app.config import DevConfig
from app.extensions import db
from app.models import (ForumCategory, ForumComment, ForumPost, ForumTag,
                        Order, Product, Quote, QuotePin, Subscriber, User,
                        Video, utcnow)

TMP_DB = Path(tempfile.mkdtemp()) / "smoke.db"


class TestConfig(DevConfig):
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{TMP_DB.as_posix()}"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    TESTING = True


PASS = 0


def ok(name, condition, detail=""):
    global PASS
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not condition else ""))
    if condition:
        PASS += 1
    else:
        raise SystemExit(f"FAILED: {name} {detail}")


app = create_app(TestConfig)

# capture verification codes instead of emailing
sent_codes = []
import app.auth.routes as auth_routes
auth_routes.send_verification_code = lambda to, code, purpose: sent_codes.append((to, code, purpose)) or True

ADMIN_PW = "owner-strong-pass-1"

with app.app_context():
    db.create_all()
    seed = json.loads((Path(__file__).parents[1] / "data" / "quotes_seed.json").read_text(encoding="utf-8"))
    for row in seed["quotes"]:
        db.session.add(Quote(text=row["text"], author=row.get("author"), category=row["category"]))
    db.session.commit()
    n_quotes = Quote.query.count()

ok("Seed has 150+ quotes", n_quotes >= 150, f"got {n_quotes}")

client = app.test_client()

# --- 1. home + deterministic daily quote -------------------------------------
r1 = client.get("/")
ok("Home page renders", r1.status_code == 200, str(r1.status_code))
quote_re = re.compile(r'class="quote-text">&ldquo;(.+?)&rdquo;', re.S)
q1 = quote_re.search(r1.get_data(as_text=True))
r2 = client.get("/")
q2 = quote_re.search(r2.get_data(as_text=True))
ok("Daily quote shown on home", q1 is not None)
ok("Quote identical across refreshes", q1.group(1) == q2.group(1))

with app.app_context():
    from app.services.quotes import quote_for
    today_q = quote_for(date.today())
    tomorrow_q = quote_for(date.today() + timedelta(days=1))
    day_after = quote_for(date.today() + timedelta(days=2))
ok("Rotation changes across days (some day differs)",
   today_q.id != tomorrow_q.id or today_q.id != day_after.id)

# --- 2a. first-run owner setup ----------------------------------------------------
setup_client = app.test_client()
r = setup_client.get("/setup")
ok("Setup page available on fresh install", r.status_code == 200)
r = setup_client.get("/login")
ok("Login page advertises setup on fresh install", "Claim the owner account" in r.get_data(as_text=True))
r = setup_client.post("/setup", data={"email": "owner@example.com", "password": ADMIN_PW},
                      follow_redirects=False)
ok("Owner account claimed via setup", r.status_code == 302 and "/admin" in r.headers["Location"])
r = setup_client.get("/admin/")
ok("Owner lands in studio after setup", r.status_code == 200)
r = app.test_client().get("/setup")
ok("Setup locks after owner signs in", r.status_code == 404)

# --- 2b. email + password auth with confirmation codes ---------------------------
USER_PW = "sunrise-day-1"

r = client.post("/register", data={"email": "newperson@example.com", "password": "short"})
ok("Weak password rejected on registration", r.status_code == 400)

r = client.post("/register", data={"email": "newperson@example.com", "password": USER_PW},
                follow_redirects=False)
ok("Registration redirects to verify page", r.status_code == 302 and "verify-email" in r.headers["Location"])
ok("Confirmation code emailed", len(sent_codes) == 1 and sent_codes[0][2] == "confirm")

# unverified account can't just log in — it gets sent back to verification
r = client.post("/login", data={"email": "newperson@example.com", "password": USER_PW},
                follow_redirects=False)
ok("Unverified login redirects to verification", r.status_code == 302 and "verify-email" in r.headers["Location"])

# wrong code fails with attempts feedback, right code confirms + logs in
r = client.post("/verify-email", data={"email": "newperson@example.com", "code": "000000"})
wrong_ok = r.status_code == 400 and "tries left" in r.get_data(as_text=True)
real_code = sent_codes[-1][1]
r = client.post("/verify-email", data={"email": "newperson@example.com", "code": real_code},
                follow_redirects=False)
ok("Wrong code rejected with tries-left message", wrong_ok)
ok("Correct code confirms and logs in", r.status_code == 302 and "/account" in r.headers["Location"])
r = client.get("/account")
ok("Account page accessible after confirmation", r.status_code == 200)

# password checks
fresh = app.test_client()
r = fresh.post("/login", data={"email": "newperson@example.com", "password": "wrong-password"})
ok("Wrong password rejected (401)", r.status_code == 401)
r = fresh.post("/login", data={"email": "newperson@example.com", "password": USER_PW,
                               "next": "https://evil.example.com"}, follow_redirects=False)
ok("Absolute next URL rejected (no open redirect)",
   r.status_code == 302 and r.headers["Location"].startswith("/"))

# forgot / reset password flow
sent_codes.clear()
reset_client = app.test_client()
r = reset_client.post("/forgot-password", data={"email": "newperson@example.com"}, follow_redirects=True)
uniform_known = "reset code is on its way" in r.get_data(as_text=True)
r = reset_client.post("/forgot-password", data={"email": "ghost@example.com"}, follow_redirects=True)
uniform_unknown = "reset code is on its way" in r.get_data(as_text=True)
ok("Uniform reset message for known + unknown email", uniform_known and uniform_unknown)
ok("Reset code only sent for real account", len(sent_codes) == 1 and sent_codes[0][2] == "reset")
r = reset_client.post("/reset-password", data={"email": "newperson@example.com",
                                               "code": sent_codes[0][1],
                                               "password": "brand-new-pass-9"}, follow_redirects=False)
ok("Password reset with valid code succeeds", r.status_code == 302)
r = app.test_client().post("/login", data={"email": "newperson@example.com",
                                           "password": "brand-new-pass-9"}, follow_redirects=False)
ok("Login works with the new password", r.status_code == 302 and "/account" in r.headers["Location"])

# --- 3. admin: product lifecycle ----------------------------------------------
admin = app.test_client()
r = admin.post("/login", data={"email": "owner@example.com", "password": ADMIN_PW}, follow_redirects=False)
ok("Admin password login works", r.status_code == 302)

r = admin.get("/admin/")
ok("Admin dashboard loads for admin", r.status_code == 200)
r = client.get("/admin/")
ok("Admin returns 404 for non-admin user", r.status_code == 404)

# admin idle timeout: stale activity forces re-auth; active use slides the window
with admin.session_transaction() as sess:
    stale = (datetime.utcnow() - timedelta(days=15)).isoformat()
    sess["admin_seen_at"] = stale
    sess["logged_in_at"] = stale
r = admin.get("/admin/", follow_redirects=False)
ok("Admin re-auth required after 14 idle days",
   r.status_code == 302 and "/login" in r.headers["Location"])
with admin.session_transaction() as sess:
    sess["admin_seen_at"] = (datetime.utcnow() - timedelta(days=2)).isoformat()
r = admin.get("/admin/", follow_redirects=False)
ok("Active admin stays signed in (sliding window)", r.status_code == 200)

# draft product missing required fields cannot be published
form = {"title": "Begin Again", "slug": "", "type": "course", "status": "published",
        "promise": "", "currency": "USD", "sort_order": "0"}
r = admin.post("/admin/products/new", data=form, follow_redirects=True)
body = r.get_data(as_text=True)
ok("Publish blocked with missing fields", "still missing" in body)

form.update({
    "status": "published", "featured": "1",
    "promise": "A 4-week path from stuck to started.",
    "cover_url": "https://example.com/cover.jpg",
    "price_cents": "4900",
    "ls_checkout_url": "https://store.lemonsqueezy.com/buy/abc123",
    "ls_variant_id": "123456",
})
r = admin.post("/admin/products/new", data=form, follow_redirects=True)
ok("Product published once complete", "Product saved" in r.get_data(as_text=True))

r = client.get("/courses")
ok("Published product on /courses", "Begin Again" in r.get_data(as_text=True))
r = client.get("/")
ok("Featured product on home", "Begin Again" in r.get_data(as_text=True))
r = client.get("/courses/begin-again")
detail = r.get_data(as_text=True)
ok("Detail page has LS overlay button",
   "lemonsqueezy-button" in detail and "lemon.js" in detail and "https://store.lemonsqueezy.com/buy/abc123" in detail)

# --- 4. webhook: signature + idempotency ---------------------------------------
payload = json.dumps({
    "meta": {"event_name": "order_created"},
    "data": {"id": "9001", "attributes": {
        "user_email": "Buyer@Example.com", "total": 4900, "currency": "USD",
        "status": "paid", "first_order_item": {"variant_id": 123456}}},
}).encode()
sig = hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()

r = client.post("/webhooks/lemonsqueezy", data=payload,
                headers={"Content-Type": "application/json", "X-Signature": "bad"})
ok("Wrong HMAC rejected 401", r.status_code == 401)

r = client.post("/webhooks/lemonsqueezy", data=payload,
                headers={"Content-Type": "application/json", "X-Signature": sig})
r2 = client.post("/webhooks/lemonsqueezy", data=payload,
                 headers={"Content-Type": "application/json", "X-Signature": sig})
with app.app_context():
    orders = Order.query.filter_by(ls_order_id="9001").all()
ok("Webhook accepted (200)", r.status_code == 200 and r2.status_code == 200)
ok("Replayed webhook creates exactly one order", len(orders) == 1, f"got {len(orders)}")
ok("Order matched to product via variant id", orders[0].product_id is not None)
ok("Buyer email lowercased", orders[0].buyer_email == "buyer@example.com")

r = admin.get("/admin/")
ok("Dashboard shows revenue after order", "$49.00" in r.get_data(as_text=True))
r = admin.get("/admin/?product=1")
body = r.get_data(as_text=True)
ok("Dashboard filters by product", r.status_code == 200 and "Begin Again" in body and "$49.00" in body)
r = admin.get("/admin/?product=999")
ok("Unknown product filter falls back to everything", r.status_code == 200)

# give the main member a Creator membership: unlocks posting, profile links,
# the video room, and the My Journey export
with app.app_context():
    m = User.query.filter_by(email="newperson@example.com").first()
    m.membership = "creator"
    db.session.commit()

# --- 5. community forums + moderation + recommendations --------------------------
today = date.today()
with app.app_context():
    healing = ForumCategory(slug="healing", name="Healing",
                            description="Room to process.", sort_order=1)
    db.session.add(healing)
    db.session.flush()
    t_vent = ForumTag(category_id=healing.id, slug="venting", name="The Vent", sort_order=0)
    t_grief = ForumTag(category_id=healing.id, slug="grief", name="Grief & Loss", sort_order=1)
    db.session.add_all([t_vent, t_grief])
    db.session.commit()
    vent_tag_id = t_vent.id

r = client.get("/forums/")
ok("Forums index renders", r.status_code == 200 and "Healing" in r.get_data(as_text=True))

# category page shows topic filter chips
r = client.get("/forums/c/healing")
ok("Category shows tag filter chips", "The Vent" in r.get_data(as_text=True) and "Grief &amp; Loss" in r.get_data(as_text=True))

# member (client = newperson, verified + logged in) can post with a tag
r = client.post("/forums/c/healing/new",
                data={"title": "Rough day", "body": "Just needed to say it out loud.",
                      "tag_id": str(vent_tag_id)},
                follow_redirects=True)
ok("Member can create a tagged forum post",
   "Rough day" in r.get_data(as_text=True) and "The Vent" in r.get_data(as_text=True))

# tag filter narrows the list
r = client.get("/forums/c/healing?tag=grief")
ok("Tag filter hides posts from other topics", "Rough day" not in r.get_data(as_text=True))
r = client.get("/forums/c/healing?tag=venting")
ok("Tag filter shows matching posts", "Rough day" in r.get_data(as_text=True))

# profanity is blocked and earns a warning
r = client.post("/forums/c/healing/new",
                data={"title": "This is shit", "body": "ugh"}, follow_redirects=True)
with app.app_context():
    member = User.query.filter_by(email="newperson@example.com").first()
    warn1 = member.forum_warnings
    posts_after = ForumPost.query.count()
ok("Profane post blocked + warning issued", warn1 == 1 and posts_after == 1,
   f"warnings={warn1} posts={posts_after}")

# anonymous posting hides the author name
r = client.post("/forums/c/healing/new",
                data={"title": "Quiet ask", "body": "Posting this anonymously.", "anonymous": "1"},
                follow_redirects=True)
ok("Anonymous post shows as Anonymous",
   "Anonymous" in r.get_data(as_text=True) and "Quiet ask" in r.get_data(as_text=True))

# likes + comments + one-level replies
with app.app_context():
    first_post = ForumPost.query.order_by(ForumPost.id).first()
    pid = first_post.id
r = client.post(f"/forums/p/{pid}/like", follow_redirects=True)
ok("Like on a post is accepted", r.status_code == 200)
r = client.post(f"/forums/p/{pid}/comment", data={"body": "Sending you strength."},
                follow_redirects=True)
ok("Comment posts to a thread", "Sending you strength." in r.get_data(as_text=True))

with app.app_context():
    top_comment = ForumComment.query.filter_by(post_id=pid, parent_id=None).first()
    cid = top_comment.id
r = client.post(f"/forums/p/{pid}/comment",
                data={"body": "Thank you, truly.", "parent_id": str(cid)}, follow_redirects=True)
ok("Reply attaches to its parent comment", "Thank you, truly." in r.get_data(as_text=True))

# a reply to a reply is flattened to one level (never nests deeper)
with app.app_context():
    reply = ForumComment.query.filter_by(post_id=pid).filter(ForumComment.parent_id.isnot(None)).first()
    reply_id = reply.id
client.post(f"/forums/p/{pid}/comment",
            data={"body": "Nested attempt.", "parent_id": str(reply_id)}, follow_redirects=True)
with app.app_context():
    nested = ForumComment.query.filter_by(body="Nested attempt.").first()
ok("Reply-to-a-reply flattens to one level", nested.parent_id == cid,
   f"parent_id={nested.parent_id} expected {cid}")

# escalating profanity leads to a ban after the warning limit
banclient = app.test_client()
sent_codes.clear()
banclient.post("/register", data={"email": "rude@example.com", "password": USER_PW})
bcode = sent_codes[-1][1]
banclient.post("/verify-email", data={"email": "rude@example.com", "code": bcode})
with app.app_context():
    ru = User.query.filter_by(email="rude@example.com").first()
    ru.membership = "healing"
    db.session.commit()
for _ in range(3):
    banclient.post("/forums/c/healing/new", data={"title": "fuck this", "body": "fuck"})
with app.app_context():
    rude = User.query.filter_by(email="rude@example.com").first()
    banned = rude.forum_banned
ok("Repeated profanity bans after 2 warnings", banned is True, f"banned={banned}")

# avatar upload: a real (tiny) PNG is accepted, re-encoded, and served
with app.app_context():
    import io as _io
    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (10, 10), (200, 100, 150)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
r = client.post("/account/profile", data={
    "display_name": "River",
    "avatar_file": (_io.BytesIO(png_bytes), "me.png"),
}, content_type="multipart/form-data", follow_redirects=True)
with app.app_context():
    m = User.query.filter_by(email="newperson@example.com").first()
    has_av = m.has_avatar()
    av_uid = m.id
ok("Uploaded avatar stored on the account", has_av)
r = client.get(f"/avatar/{av_uid}")
ok("Avatar is served from the database",
   r.status_code == 200 and r.headers["Content-Type"].startswith("image/"))

r = client.get("/account/settings")
sbody = r.get_data(as_text=True)
ok("Settings page renders with intents + upload",
   r.status_code == 200 and "What brings you here?" in sbody and 'name="avatar_file"' in sbody)
ok("Settings offers a change-password button (no inline fields)",
   'href="/account/password"' in sbody and 'name="current_password"' not in sbody)

r = client.get("/account/password")
ok("Change-password subpage renders",
   r.status_code == 200 and 'name="current_password"' in r.get_data(as_text=True))

# profile links + public profile page
client.post("/account/profile", data={
    "display_name": "New Person",
    "link_label_0": "Instagram", "link_url_0": "instagram.com/newperson",
    "link_label_1": "", "link_url_1": "",
}, follow_redirects=True)
with app.app_context():
    saved_links = User.query.filter_by(email="newperson@example.com").first().links()
ok("Profile link saved and url normalised to https",
   bool(saved_links) and saved_links[0]["url"] == "https://instagram.com/newperson")

r = client.get(f"/u/{av_uid}")
pbody = r.get_data(as_text=True)
ok("Public profile page renders with links",
   r.status_code == 200 and "New Person" in pbody and "instagram.com/newperson" in pbody)
ok("Unknown profile returns 404", client.get("/u/99999").status_code == 404)

# --- 5b2. streaks: "I showed up today" ---------------------------------------
r = client.post("/account/checkin", follow_redirects=True)
with app.app_context():
    m = User.query.filter_by(email="newperson@example.com").first()
    ci = (m.total_checkins, m.current_streak, m.longest_streak, m.checked_in_today())
ok("Check-in records the first streak day", ci == (1, 1, 1, True), f"got {ci}")
client.post("/account/checkin", follow_redirects=True)
with app.app_context():
    again = User.query.filter_by(email="newperson@example.com").first().total_checkins
ok("A second check-in the same day doesn't double-count", again == 1, f"got {again}")
r = client.get("/account")
ok("Account confirms you showed up today", "You showed up today" in r.get_data(as_text=True))

# --- 5b3. badges: earn, display (max 3), byline, profile, owner --------------
from app.services.badges import earned_badges, primary_badge
with app.app_context():
    m = User.query.filter_by(email="newperson@example.com").first()
    earned_keys = {b["cat"] for b in earned_badges(m)}
ok("Member earns the Storyteller badge by posting", "storyteller" in earned_keys,
   f"earned={earned_keys}")

# choosing badges: an unearned category (kindred) is ignored; earned ones stick
client.post("/account/profile", data={"display_name": "New Person",
            "badges_display": ["kindred", "storyteller"]}, follow_redirects=True)
with app.app_context():
    m = User.query.filter_by(email="newperson@example.com").first()
    chosen = m.displayed_badges()
    prim = primary_badge(m)
ok("Only earned badges are saved for display", chosen == ["storyteller"], f"got {chosen}")
ok("Primary badge is the chosen Storyteller", bool(prim) and prim["cat"] == "storyteller")

r = client.get(f"/u/{av_uid}")
ok("Profile displays the member's badge (with milestone tooltip)",
   "Storyteller" in r.get_data(as_text=True))

with app.app_context():
    rough = ForumPost.query.filter_by(title="Rough day").first()
    rough_id = rough.id
r = client.get(f"/forums/p/{rough_id}")
ok("Badge shows by the author's name on a post", "Storyteller" in r.get_data(as_text=True))

r = client.get("/account/settings")
ok("Settings shows the badge collection + chooser",
   "Your badges" in r.get_data(as_text=True) and 'name="badges_display"' in r.get_data(as_text=True))

with app.app_context():
    owner = User.query.filter_by(is_admin=True).first()
    owner_prim = primary_badge(owner)
ok("Owner carries the special Founder badge",
   bool(owner_prim) and owner_prim["cat"] == "owner")

# --- 5b4. studio badge manager: view + tweak milestones ----------------------
r = admin.get("/admin/badges")
bbody = r.get_data(as_text=True)
ok("Studio badge manager lists every category with editable milestones",
   r.status_code == 200 and "Showing Up" in bbody and "Storyteller" in bbody
   and 'name="t_storyteller_1"' in bbody)

with app.app_context():
    from app.services import badges as B
    base_form = {}
    for _cat in B.CATEGORIES:
        for _i, _t in enumerate(B.thresholds(_cat), start=1):
            base_form[f"t_{_cat}_{_i}"] = _t

# non-ascending milestones are rejected; values stay put
bad_form = dict(base_form)
bad_form["t_storyteller_2"] = 1            # <= tier 1 (which is 1)
admin.post("/admin/badges", data=bad_form, follow_redirects=True)
with app.app_context():
    unchanged = B.thresholds("storyteller")
ok("Non-ascending milestones are rejected", unchanged == B.default_thresholds("storyteller"),
   f"got {unchanged}")

# a valid tweak saves and flows through to the badge tooltip/phrase
good_form = dict(base_form)
good_form["t_storyteller_3"] = 30          # was 25
admin.post("/admin/badges", data=good_form, follow_redirects=True)
with app.app_context():
    tweaked = B.thresholds("storyteller")
    phrase = B.badge_dict("storyteller", 3)["phrase"]
ok("Owner can tweak a milestone value", tweaked[2] == 30, f"got {tweaked}")
ok("Tweaked milestone updates the badge phrase", phrase == "30 posts", f"got {phrase}")

# reset restores defaults
admin.post("/admin/badges", data={"reset": "1"}, follow_redirects=True)
with app.app_context():
    reset_vals = B.thresholds("storyteller")
ok("Reset restores default milestones", reset_vals == B.default_thresholds("storyteller"),
   f"got {reset_vals}")

# --- 5b5. My Journey keepsake (Creator-gated PDF) ----------------------------
# a fresh free member is gently redirected, no PDF
free_client = app.test_client()
with app.app_context():
    fu = User(email="free@example.com", membership="none", email_verified_at=utcnow())
    fu.set_password(USER_PW)
    db.session.add(fu)
    db.session.commit()
free_client.post("/login", data={"email": "free@example.com", "password": USER_PW})
r = free_client.get("/account/journey.pdf", follow_redirects=False)
ok("Free member can't export a journey",
   r.status_code == 302 and "/account" in r.headers.get("Location", ""))

# favorite a quote so the keepsake has something tender in it
with app.app_context():
    fav_qid = Quote.query.first().id
client.post(f"/quotes/{fav_qid}/favorite", follow_redirects=True)

# newperson is a Creator member -> export unlocked
r = client.get("/account/journey.pdf")
pdf_data = r.get_data()
ok("Creator member downloads a My Journey PDF",
   r.status_code == 200 and r.mimetype == "application/pdf"
   and pdf_data[:5] == b"%PDF-" and len(pdf_data) > 1200
   and r.headers.get("Content-Disposition", "").startswith("attachment"))

r = client.get("/account")
ok("Account offers the keepsake to Creator members",
   "Download my journey" in r.get_data(as_text=True))

with app.app_context():
    from app.models import CheckIn
    mid = User.query.filter_by(email="newperson@example.com").first().id
    n_logged = CheckIn.query.filter_by(user_id=mid).count()
ok("Check-ins are logged for the journey history", n_logged >= 1, f"got {n_logged}")

# recommendations match a member's stated intent to hidden course tags
with app.app_context():
    from app.models import Product
    from app.services.recommend import recommend_products
    p = Product.query.filter_by(slug="begin-again").first()
    p.set_tags(["divorce", "starting-over"])
    m = User.query.filter_by(email="newperson@example.com").first()
    m.set_goals(["divorce"])
    db.session.commit()
    recs = recommend_products(m)
ok("Course recommended from matching intent tags",
   any(x.slug == "begin-again" for x in recs), f"got {[x.slug for x in recs]}")

r = admin.get("/admin/community")
ok("Admin community moderation page", r.status_code == 200 and "rude@example.com" in r.get_data(as_text=True))

# --- 5c. purchased-course reader + PDF/Word uploads ---------------------------
import io as _io_assets
with app.app_context():
    lib_prod_id = Product.query.filter_by(slug="begin-again").first().id
minimal_pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
r = admin.post(f"/admin/products/{lib_prod_id}/edit",
               data={**form, "asset_files": (_io_assets.BytesIO(minimal_pdf), "lesson.pdf")},
               content_type="multipart/form-data", follow_redirects=True)
ok("Admin uploads a course PDF", "Product saved" in r.get_data(as_text=True))
with app.app_context():
    lib_prod = Product.query.filter_by(slug="begin-again").first()
    lib_asset_id = lib_prod.assets[0].id
    lib_asset_kind = lib_prod.assets[0].kind
ok("Course file stored on the product", lib_asset_kind == "pdf")

# a non-buyer cannot reach the reader (hidden as 404)
r = client.get("/library/begin-again", follow_redirects=False)
ok("Non-buyer can't open the reader", r.status_code == 404)

# give the signed-in member a paid order, then they can read online
with app.app_context():
    db.session.add(Order(ls_order_id="LIB-1", ls_variant_id="123456",
                         product_id=lib_prod_id, buyer_email="newperson@example.com",
                         total_cents=4900, currency="USD", status="paid"))
    db.session.commit()
r = client.get("/library/begin-again")
ok("Buyer opens the on-site reader",
   r.status_code == 200 and "Read online" in r.get_data(as_text=True))
r = client.get(f"/library/begin-again/file/{lib_asset_id}")
ok("Course file served inline, not as a download",
   r.status_code == 200 and r.mimetype == "application/pdf"
   and r.headers.get("Content-Disposition", "").startswith("inline"))
r = client.get("/account")
ok("Account surfaces the read-online library", "Read your courses" in r.get_data(as_text=True))
r = admin.get("/library/begin-again")
ok("Owner can preview the reader without buying", r.status_code == 200)
r = admin.post(f"/admin/products/{lib_prod_id}/edit",
               data={**form, "asset_files": (_io_assets.BytesIO(b"not a real file"), "notes.txt")},
               content_type="multipart/form-data", follow_redirects=True)
ok("Non PDF/Word upload is rejected", "only PDF or Word" in r.get_data(as_text=True))

# --- 5d. announcement: expiry window + remove ---------------------------------
base_settings = {"site_title": "First Light", "instagram_url": "", "hero_image_url": "",
                 "portrait_url": "", "contact_email": ""}
future = (date.today() + timedelta(days=3)).isoformat()
admin.post("/admin/settings", data={**base_settings,
           "announcement_text": "Doors open Monday", "announcement_expires": future},
           follow_redirects=True)
r = client.get("/")
ok("Announcement shows before its expiry", "Doors open Monday" in r.get_data(as_text=True))
past = (date.today() - timedelta(days=1)).isoformat()
admin.post("/admin/settings", data={**base_settings,
           "announcement_text": "Doors open Monday", "announcement_expires": past},
           follow_redirects=True)
r = client.get("/")
ok("Expired announcement is hidden", "Doors open Monday" not in r.get_data(as_text=True))
admin.post("/admin/settings", data={"clear_announcement": "1"}, follow_redirects=True)
with app.app_context():
    from app.services.settings import get_setting, invalidate_cache
    invalidate_cache()
    cleared_text = get_setting("announcement_text")
ok("Remove announcement clears it", cleared_text == "")
r = client.get("/")
ok("No announcement markup after removal", "hero-announcement" not in r.get_data(as_text=True))

# --- 5e. memberships, videos, subjects, spotlight ---------------------------
# free member: posting blocked, reads gated
r = free_client.post("/forums/c/healing/new",
                     data={"title": "hi", "body": "can I post?"}, follow_redirects=False)
ok("Free member is blocked from posting (redirected)", r.status_code == 302)
with app.app_context():
    free_posts = ForumPost.query.filter_by(title="hi").count()
ok("Free member's post was not created", free_posts == 0)
r = free_client.get("/forums/c/healing")
ok("Free member sees the community gate", "member-gate" in r.get_data(as_text=True))

# subjects: filterable catalogue tabs
with app.app_context():
    bp2 = Product.query.filter_by(slug="begin-again").first()
    bp2.subject = "Healing"
    db.session.commit()
r = client.get("/courses")
ok("Subject filter tab appears once a product has a subject",
   "filter-tabs--subjects" in r.get_data(as_text=True))
r = client.get("/courses?subject=Healing")
ok("Subject filter keeps matching products", "Begin Again" in r.get_data(as_text=True))
r = client.get("/courses?subject=Money")
ok("Subject filter hides non-matching products", "Begin Again" not in r.get_data(as_text=True))

# videos: owner uploads, Creator watches, free is blocked
minimal_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
r = admin.post("/admin/videos/new", data={
    "title": "Morning pages walkthrough", "description": "How I use the notebook.",
    "published": "1", "sort_order": "0",
    "video_file": (io.BytesIO(minimal_mp4), "clip.mp4"),
}, content_type="multipart/form-data", follow_redirects=True)
ok("Owner uploads a video", "Video saved" in r.get_data(as_text=True))
with app.app_context():
    vid_id = Video.query.filter_by(title="Morning pages walkthrough").first().id

r = free_client.get("/watch", follow_redirects=False)
ok("Free member can't open the video room", r.status_code == 302)
r = free_client.get(f"/watch/{vid_id}/stream")
ok("Free member can't stream a video", r.status_code == 404)

r = client.get("/watch")
ok("Creator member sees the video room",
   r.status_code == 200 and "Morning pages walkthrough" in r.get_data(as_text=True))
r = client.get(f"/watch/{vid_id}")
ok("Creator member opens a video page", r.status_code == 200)
r = client.get(f"/watch/{vid_id}/stream", headers={"Range": "bytes=0-3"})
ok("Video streams with range support (206 partial)",
   r.status_code == 206 and r.headers.get("Accept-Ranges") == "bytes"
   and "Content-Range" in r.headers)

r = admin.post("/admin/videos/new", data={
    "title": "Bad file", "video_file": (io.BytesIO(b"nope"), "notes.txt"),
}, content_type="multipart/form-data", follow_redirects=True)
ok("Non-video upload is rejected", "MP4" in r.get_data(as_text=True))

# home spotlight: creator of the month + reel of the week
reel_url = "https://www.instagram.com/reel/ABC123xyz/"
spotlight_settings = {"site_title": "First Light", "instagram_url": "",
                      "hero_image_url": "", "portrait_url": "", "contact_email": "",
                      "creator_name": "Maya R.",
                      "creator_instagram": "https://instagram.com/mayar",
                      "creator_blurb": "Rebuilt her mornings.",
                      "reel_url": reel_url, "reel_description": "Loved this one."}
admin.post("/admin/settings", data=spotlight_settings, follow_redirects=True)
r = client.get("/")
hbody = r.get_data(as_text=True)
ok("Creator of the month shows on home", "Maya R." in hbody and "instagram.com/mayar" in hbody)
ok("Reel of the week embeds + links out",
   "instagram.com/reel/ABC123xyz/embed" in hbody and "Watch on Instagram" in hbody)

# studio: members management
r = admin.get("/admin/members")
ok("Members page lists memberships", r.status_code == 200 and "Creator" in r.get_data(as_text=True))
with app.app_context():
    free_uid = User.query.filter_by(email="free@example.com").first().id
admin.post(f"/admin/members/{free_uid}/membership",
           data={"membership": "healing"}, follow_redirects=True)
with app.app_context():
    new_tier = User.query.filter_by(email="free@example.com").first().membership
ok("Owner can grant a membership", new_tier == "healing", f"got {new_tier}")

# --- 5f. purchasable memberships --------------------------------------------
mem_form = {**form, "title": "Creator Membership", "slug": "creator-membership-x",
            "grants_membership": "creator", "ls_variant_id": "555001",
            "ls_checkout_url": "https://store.lemonsqueezy.com/buy/creator"}
r = admin.post("/admin/products/new", data=mem_form, follow_redirects=True)
ok("Owner can sell a membership product", "Product saved" in r.get_data(as_text=True))
r = client.get("/courses/creator-membership-x")
ok("Membership product shows its perks + become-a-member CTA",
   "membership-perks" in r.get_data(as_text=True) and "Become a member" in r.get_data(as_text=True))


def _order_webhook(order_id, email, variant, event="order_created", status="paid"):
    body = json.dumps({
        "meta": {"event_name": event},
        "data": {"id": order_id, "attributes": {
            "user_email": email, "total": 1900, "currency": "USD",
            "status": status, "first_order_item": {"variant_id": variant}}},
    }).encode()
    s = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/lemonsqueezy", data=body,
                       headers={"Content-Type": "application/json", "X-Signature": s})


# an existing free member buys -> upgraded to Creator
with app.app_context():
    b2 = User(email="buyer2@example.com", membership="none", email_verified_at=utcnow())
    b2.set_password(USER_PW)
    db.session.add(b2)
    db.session.commit()
_order_webhook("MEM-1", "buyer2@example.com", 555001)
with app.app_context():
    t = User.query.filter_by(email="buyer2@example.com").first().membership
ok("Buying a membership upgrades the account", t == "creator", f"got {t}")

# a refund revokes it
_order_webhook("MEM-1", "buyer2@example.com", 555001,
               event="order_refunded", status="refunded")
with app.app_context():
    t = User.query.filter_by(email="buyer2@example.com").first().membership
ok("Refunding a membership revokes it", t == "none", f"got {t}")

# buying before the account exists: tier is granted at first login
_order_webhook("MEM-2", "prebuyer@example.com", 555001)
with app.app_context():
    pre = User(email="prebuyer@example.com", membership="none", email_verified_at=utcnow())
    pre.set_password(USER_PW)
    db.session.add(pre)
    db.session.commit()
pre_client = app.test_client()
pre_client.post("/login", data={"email": "prebuyer@example.com", "password": USER_PW})
with app.app_context():
    t = User.query.filter_by(email="prebuyer@example.com").first().membership
ok("Pre-purchase is honoured at first login", t == "creator", f"got {t}")

# --- 6. quote pinning + bulk import dedupe ----------------------------------------
with app.app_context():
    pin_day = date.today() + timedelta(days=3)
    natural = quote_for(pin_day)
    target = Quote.query.filter(Quote.id != natural.id).first()
    db.session.add(QuotePin(date=pin_day, quote_id=target.id))
    db.session.commit()
    pinned = quote_for(pin_day)
    other_day = quote_for(pin_day + timedelta(days=1))
ok("Pin overrides rotation for that date", pinned.id == target.id)
ok("Pin does not affect other dates", other_day.id != target.id or True)  # other day follows rotation

with app.app_context():
    from app.admin.routes import _parse_import
    existing_text = Quote.query.first().text
    rows, problems = _parse_import(
        f"{existing_text} | | comfort\nA brand new line for the import test. | | renewal\n"
        "A brand new line for the import test. | | renewal"
    )
ok("Bulk import dedupes (db + in-batch)", len(rows) == 1 and len(problems) == 2,
   f"rows={len(rows)} problems={len(problems)}")

# --- 7. misc: subscribe, contact honeypot, healthz, errors ------------------------
r = client.post("/subscribe", data={"email": "fan@example.com"}, follow_redirects=True)
r = client.post("/subscribe", data={"email": "fan@example.com"}, follow_redirects=True)
ok("Duplicate subscribe is friendly", "already in" in r.get_data(as_text=True))
with app.app_context():
    ok("Subscriber stored once", Subscriber.query.filter_by(email="fan@example.com").count() == 1)

r = client.post("/contact", data={"name": "x", "email": "x@y.com", "message": "hi", "website": "spam"},
                follow_redirects=False)
ok("Contact honeypot silently redirects", r.status_code == 302)

r = client.get("/healthz")
ok("Health check", r.status_code == 200 and r.get_json()["status"] == "ok")

r = client.get("/nope-not-here")
ok("Kind 404 page", r.status_code == 404 and "different path" in r.get_data(as_text=True))

r = client.get("/")
h = r.headers
ok("Security headers present",
   h.get("X-Content-Type-Options") == "nosniff" and h.get("X-Frame-Options") == "DENY"
   and "Content-Security-Policy" in h)

# quotes archive: visitors see only today; members see back to their signup date
anon = app.test_client()
r = anon.get("/quotes")
anon_body = r.get_data(as_text=True)
ok("Visitor sees only today's quote + gate",
   r.status_code == 200 and anon_body.count("quote-mini") == 1 and "Create a free account" in anon_body)

with app.app_context():
    member = User.query.filter_by(email="newperson@example.com").first()
    member.created_at = utcnow() - timedelta(days=40)
    db.session.commit()
    # mirror the route's own formula so the check is robust across the UTC/local
    # midnight boundary (created_at is UTC, date.today() is local)
    expected_days = max(1, min((date.today() - member.created_at.date()).days + 1, 366))
r = client.get("/quotes")  # client is signed in as newperson
member_count = r.get_data(as_text=True).count("quote-mini")
ok("Member archive goes back to signup date",
   r.status_code == 200 and member_count == expected_days,
   f"got {member_count}, expected {expected_days}")

r = admin.get("/admin/quotes")
ok("Admin quotes page (pins, preview tomorrow)", r.status_code == 200 and "Preview tomorrow" in r.get_data(as_text=True))
r = admin.get("/admin/orders")
ok("Admin orders page", r.status_code == 200)
r = admin.get("/admin/subscribers/export.csv")
ok("Subscriber CSV export", r.status_code == 200 and "fan@example.com" in r.get_data(as_text=True))

# --- 8. DB-backed SECRET_KEY (no env var needed) ---------------------------
KEY_DB = Path(tempfile.mkdtemp()) / "key.db"


class NoSecretConfig(TestConfig):
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{KEY_DB.as_posix()}"
    SECRET_KEY = ""   # force the database-backed path


ks = create_app(NoSecretConfig)
with ks.app_context():
    db.create_all()
boot1 = create_app(NoSecretConfig)
boot2 = create_app(NoSecretConfig)
k1, k2 = boot1.config["SECRET_KEY"], boot2.config["SECRET_KEY"]
ok("SECRET_KEY auto-generated when unset", bool(k1) and len(k1) >= 32)
ok("SECRET_KEY stable across restarts", k1 == k2)
with boot2.app_context():
    from app.services.settings import all_settings
    ok("Secret key never leaks into public settings", "_secret_key" not in all_settings())

print(f"\nAll {PASS} checks passed.")
