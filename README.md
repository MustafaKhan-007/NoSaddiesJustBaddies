# First Light

A warm, mobile-first storefront for digital courses and notebook guides, built to
replace Gumroad. Flask + PostgreSQL, with **Lemon Squeezy hosted checkout** as the
merchant of record (payments, tax, and file delivery all happen on their side —
this site never touches card data and stores no files).

What's inside:

- Full catalog with filterable shop, rich product pages, and overlay checkout
- Daily motivational quote with deterministic rotation, pinning, and a
  kind-by-design check-in streak system
- Email + password accounts with 6-digit email confirmation codes on
  registration, plus code-based password reset
- Admin studio: dashboard with revenue charts, product/quote/testimonial/FAQ/page
  management, subscriber & order CSV exports, site settings
- Lemon Squeezy webhook receiver (signed, idempotent) + manual API reconciliation

---

## 1. Local setup

Requires Python 3.12+.

```bash
git clone <this repo> && cd <this repo>
python -m venv .venv
# Windows:  .venv\Scripts\activate     macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env        # cp on macOS/Linux (defaults work for local dev)

# create the SQLite dev database (no DATABASE_URL needed locally)
set FLASK_APP=app:create_app  # PowerShell: $env:FLASK_APP = "app:create_app"
flask db upgrade

# load 150 quotes + starter FAQ/legal stubs (content only — no credentials)
python seed.py

flask run
```

Open http://localhost:5000/setup and **claim the owner account** in the
browser (choose your email + password). The setup page locks itself as soon
as the owner has signed in once; after that you manage everything from
`/admin`, and password changes (yours included) always stick — nothing ever
resets them on deploy. **Email in dev:** when `SMTP_HOST` is empty, every
email (including registration confirmation codes) is printed to the terminal
running `flask run`.

### Environment variables

See `.env.example` for the full annotated list. The set is deliberately tiny.
In production only these are **required** (the app refuses to boot otherwise):

- `DATABASE_URL` — the managed Postgres connection string.
- `MAIL_FROM` — the verified "From" address for emails.
- **one email transport** — either `BREVO_API_KEY` (HTTP API, works everywhere)
  or all four of `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`.

Everything else is optional or auto-managed:

- `SECRET_KEY` — auto-generated and stored in the database if unset (still
  honored if you provide one).
- `APP_ENV` — auto-detected as production on Render.
- `LEMONSQUEEZY_WEBHOOK_SECRET` / `LEMONSQUEEZY_API_KEY` — set these only when
  wiring up payments. Until then the storefront works and webhooks are rejected.
- Contact-form messages go to whoever owns the admin account (claimed at
  `/setup`) — there's no separate admin-email variable.

> **Render free tier note:** Render blocks outbound SMTP ports (25/465/587)
> on free web services, so Gmail/any SMTP relay will time out there. Use
> `BREVO_API_KEY` instead (free Brevo account, ~300 emails/day), or upgrade
> the Render service to a paid instance to unblock SMTP.

---

## 2. Lemon Squeezy setup

1. **API key** — LS dashboard → *Settings → API* → create a key →
   `LEMONSQUEEZY_API_KEY`. Used only by the dashboard "Sync with Lemon Squeezy"
   button (drift repair); day-to-day order data arrives via webhooks.
2. **Webhook** — *Settings → Webhooks → "+"*:
   - Callback URL: `https://<your-app>.onrender.com/webhooks/lemonsqueezy`
   - Signing secret: any long random string → also set it as
     `LEMONSQUEEZY_WEBHOOK_SECRET` on the server
   - Subscribe to: `order_created` and `order_refunded`
3. **Per product** (in this site's admin → Products):
   - **Buy link**: LS product → *Share* → copy the checkout/buy link → paste
     into "Lemon Squeezy buy link". Buttons use `lemon.js`, so checkout opens
     as an overlay on your page (plain link if JS is off).
   - **Variant ID**: LS product → *Variants* tab → the variant's ID → paste
     into "Lemon Squeezy variant ID". This is how webhook orders are matched
     to the product for your dashboard stats.
4. **PayPal** — enable it once in LS *Settings → Payment methods*; it appears
   at checkout automatically, no code change.

To test the webhook locally, send a signed request:

```bash
python - <<'PY'
import hmac, hashlib, json, urllib.request
secret = b"change-me-too"   # your LEMONSQUEEZY_WEBHOOK_SECRET
body = json.dumps({"meta": {"event_name": "order_created"},
  "data": {"id": "1001", "attributes": {"user_email": "buyer@example.com",
  "total": 2900, "currency": "USD", "status": "paid",
  "first_order_item": {"variant_id": 123456}}}}).encode()
sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://localhost:5000/webhooks/lemonsqueezy",
  data=body, headers={"Content-Type": "application/json", "X-Signature": sig})
print(urllib.request.urlopen(req).read())
PY
```

---

## 3. Deploying to Render

`render.yaml` defines everything (web service + managed Postgres):

1. Push the repo to GitHub.
2. In Render: *New → Blueprint*, pick the repo. Render creates the web service
   and the database, wiring `DATABASE_URL` automatically.
3. Fill in the `sync: false` env vars (secrets) in the Render dashboard.
4. Deploy. The **build** just installs dependencies
   (`pip install -r requirements.txt`). The **start** command runs
   `flask db upgrade && python seed.py && gunicorn "app:create_app()" --workers 2 --threads 4 --timeout 60`
   — migrations + content seeding (quotes, FAQ/legal stubs) run at *runtime*,
   because Render's internal `DATABASE_URL` hostname only resolves once the
   service is live (it is unreachable during the build phase). The seed is
   idempotent and **never touches accounts or passwords**, so re-deploys are
   safe. Health checks are on `/healthz`.

   > If you created the service manually (not via the Blueprint), set the
   > **Build Command** and **Start Command** above by hand in the dashboard,
   > and make sure `FLASK_APP=app:create_app` is set.
5. Visit `https://<your-app>.onrender.com/setup` right after the first deploy
   and claim the owner account (email + password, chosen in the browser).
   The page locks itself once the owner has signed in — do this promptly.
6. Point the Lemon Squeezy webhook (section 2) at your Render URL.

### Things to know about Render

- **The disk is ephemeral.** It's wiped on every deploy/restart. That's why
  product images are pasted URLs (Instagram CDN, Imgur, Cloudinary, ...) and all
  content lives in Postgres; nothing is ever written to local disk.
- **Rate limits reset on restart.** Flask-Limiter uses in-memory storage, which
  is fine at this scale — but note that a deploy clears the counters.
- Render terminates TLS; the app sets secure cookies and
  `PREFERRED_URL_SCHEME=https` in production.
- Logs (auth events, webhook failures) go to stdout — visible in Render's log tab.

---

## 4. How the daily quote works

- `quote_for(date)` picks deterministically: pinned quote if one exists for the
  date, otherwise `sha256(date) % len(active quotes)` after a weekday tone
  filter (Mon/Tue lean *determination*, Sat/Sun lean *comfort*). Same quote for
  everyone all day; changes at midnight; survives restarts.
- Admin → Quotes lets her add, edit, deactivate, bulk-import
  (`text | author | category` per line, deduped with preview), pin a quote to a
  launch date, and preview tomorrow's pick.
- Streaks are gentle by design: one missed day per rolling 7 is a "rest day"
  and doesn't reset the streak.

## 5. Security notes

- Passwords: hashed with werkzeug (scrypt), minimum 8 characters, never logged,
  never stored in env vars, and never reset by deploys or the seed script.
- Owner bootstrap: the one-time `/setup` page creates the admin in the browser
  and locks itself permanently after the owner's first sign-in. Claim it right
  after the first deploy.
- Email codes (confirmation + password reset): 6 random digits, only the
  SHA-256 hash stored, single-use, 15-minute expiry, max 5 wrong attempts per
  code. Password reset uses uniform responses (no account enumeration) and
  `next` is restricted to relative paths.
- Rate limits: 20/hour on login and code entry, 5/minute per email on login,
  10/hour on registration, 3/email/hour on reset requests.
- Sessions: `Secure`/`HttpOnly`/`SameSite=Lax`, 30-day remember cookie. Admin
  routes require `is_admin` **and** a login fresher than 24h, and return 404 to
  everyone else.
- CSRF on every form (webhook route exempt — it's authenticated by HMAC
  signature instead, verified with constant-time compare).
- All admin-entered Markdown is sanitized (bleach allow-list) before rendering.
- Security headers + a conservative CSP (self + Google Fonts + Lemon Squeezy +
  jsDelivr; `img-src https:` since product images are admin-pasted URLs).
