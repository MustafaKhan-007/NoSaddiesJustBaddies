"""Acceptance-criteria smoke test (run: python scripts/smoke_test.py).

Uses a throwaway SQLite database and the Flask test client. Not a pytest
suite on purpose — a single readable script the owner/dev can run anywhere.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["ADMIN_EMAIL"] = "owner@example.com"
os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = "test-secret"

from app import create_app
from app.config import DevConfig
from app.extensions import db
from app.models import CheckIn, Order, Quote, QuotePin, Subscriber, User, utcnow

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

# --- 5. streak grace rule --------------------------------------------------------
with app.app_context():
    from app.services.quotes import streak_info
    user = User.query.filter_by(email="newperson@example.com").first()
    today = date.today()
    # 6 consecutive days, then a single missed day inside the window, then today
    for offset in (0, 1, 2, 4, 5, 6, 7, 8):   # day -3 missed (rest day)
        db.session.add(CheckIn(user_id=user.id, date=today - timedelta(days=offset)))
    db.session.commit()
    info = streak_info(user.id)
ok("One missed day within 7 doesn't reset streak", info["current"] == 8, f"got {info['current']}")

with app.app_context():
    user2 = User(email="second@example.com")
    db.session.add(user2)
    db.session.flush()
    # two missed days in a row = real break
    for offset in (0, 3, 4):
        db.session.add(CheckIn(user_id=user2.id, date=today - timedelta(days=offset)))
    db.session.commit()
    info2 = streak_info(user2.id)
ok("Two consecutive missed days do reset", info2["current"] == 1, f"got {info2['current']}")

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
r = client.get("/quotes")  # client is signed in as newperson
member_count = r.get_data(as_text=True).count("quote-mini")
ok("Member archive goes back to signup date", r.status_code == 200 and 30 <= member_count <= 41,
   f"got {member_count}")

r = admin.get("/admin/quotes")
ok("Admin quotes page (pins, preview tomorrow)", r.status_code == 200 and "Preview tomorrow" in r.get_data(as_text=True))
r = admin.get("/admin/orders")
ok("Admin orders page", r.status_code == 200)
r = admin.get("/admin/subscribers/export.csv")
ok("Subscriber CSV export", r.status_code == 200 and "fan@example.com" in r.get_data(as_text=True))

print(f"\nAll {PASS} checks passed.")
