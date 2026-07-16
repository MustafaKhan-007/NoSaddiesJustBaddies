# Bloom Anyway

A warm, mobile-first storefront for digital courses and notebook guides, built to
replace Gumroad. Flask + PostgreSQL, with **Lemon Squeezy hosted checkout** as the
merchant of record (payments, tax, and file delivery all happen on their side —
this site never touches card data and stores no files). The brand mark is an inline
SVG wordmark with two minimalist sunflowers standing in for the "o"s in *bloom*.

What's inside:

- Full catalog with filterable shop (by type **and** subject tabs), rich product
  pages, and overlay checkout
- **Gift a course/guide** to a friend at checkout: the recipient's account email
  rides along as Lemon Squeezy custom data and is granted access on payment
- On-site reader for purchased courses & guides: owners upload PDF/Word files,
  buyers read them online (PDFs embedded, .docx rendered inline) with no download
- Purchasable membership tiers (Free / Healing / Creator) — sold on their own at
  `/membership`, chosen from the signup page and managed (change/cancel) from
  Settings, auto-granted on a paid order (revoked on refund)
- **Marketplace** (`/marketplace`): members advertise digital products & services
  (two categories), we redirect buyers to the seller's own site — Healing runs one
  active listing, Creator unlimited, cancelled members' listings are auto-hidden;
  filters (search, tags, location), popularity sort, and a list/tile view toggle
- Owner-uploaded **Content Library** (`/watch`): Healing members browse titles,
  thumbnails & descriptions but hit a lock icon on play; Creator members watch
  (range-streamed, no downloads)
- Home-page spotlight: **Creator of the Month** (links to Instagram) and an
  embedded **Reel of the Week** with an owner note and a watch-on-Instagram link
- Daily motivational quote with deterministic rotation and pinning
- Daily "I showed up today" streaks and evolving SVG achievement badges
  (shown on profiles and next to names in the community)
- "My Journey" premium PDF keepsake: a member's streaks, check-ins, and
  favorite quotes, beautifully laid out to keep and share
- Email + password accounts with 6-digit email confirmation codes on
  registration, plus code-based password reset
- Personalized onboarding ("what brings you here?") that quietly matches members
  to courses via hidden, admin-only product tags
- Member profiles: display name, uploaded avatar (stored in DB), short bio,
  default-anonymous toggle
- Two community forums (Building & Healing) with per-forum topic tags (readers
  filter, authors label), posts, comments with one level of replies, and likes;
  a kindness guard blocks profanity, warns twice, then pauses posting
- Admin studio: dashboard with revenue charts, product/quote/testimonial/FAQ/page
  management, community moderation, subscriber & order CSV exports, site settings
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
   `flask db upgrade && python seed.py && gunicorn "app:create_app()" --workers 2 --threads 4 --timeout 300`
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

## 4b. Community & recommendations

- New members pick from gentle "what brings you here?" intents at sign-up (and
  can change them in their settings). Each intent maps to keyword tags.
- Products carry hidden tags (Admin → product form → "Recommendation tags").
  These never render on the site; they power the "picked for where you are"
  shelf on the member's account by matching intents to tags.
- Forums live under `/forums`: two seeded forums (Building, Healing), each with
  topic tags. Readers filter a forum by tag; authors pick a tag when posting.
  Threads allow one level of replies (a reply can't be replied to — it flattens
  to the top-level comment). One-per-member likes on posts and comments. Members
  may post/comment/reply anonymously (per item, or by default via their settings).
- Avatars are uploaded (JPG/PNG/WEBP/GIF, ≤6 MB), re-encoded to a square JPEG
  with Pillow, and stored in the database so they survive Render's ephemeral
  disk. They're served from `/avatar/<user_id>`. Click the avatar in Settings to
  upload; a subpage at `/account/password` handles password changes.
- Members can add up to five custom links (Instagram, their own courses, a
  website…) in Settings. Non-anonymous authors' names in the forums link to a
  public profile at `/u/<user_id>` showing their avatar, bio, and links;
  anonymous posts expose no profile.
- Kindness guard (`app/services/moderation.py`): profane content is blocked
  (never stored), the author is warned twice, and the next offense pauses their
  posting. Admin → Community shows recent posts (removable) and warned/paused
  members (with a one-click "fresh start" to clear warnings).

## 4c. Course files & the on-site reader

- On a product (Admin → product form → "Course files") the owner uploads PDF or
  Word files. They're validated (`app/services/assets.py`), capped at 25 MB each,
  and stored in the database (`product_assets`) so they survive Render deploys.
- Buyers read them at `/library/<slug>`. Access is gated by `_owns_product`: the
  studio owner (for preview) or anyone with a **paid** order whose email matches
  their account. Non-buyers get a 404 (the reader's existence is hidden).
- Files are served from `/library/<slug>/file/<id>` with `Content-Disposition:
  inline` and `Cache-Control: private, no-store` — there is no download link.
  PDFs embed in an iframe (toolbar hidden); `.docx` is converted to sanitized
  HTML with `mammoth` + `bleach`. (Legacy `.doc` can't be previewed — prefer
  PDF/`.docx`.) Owned products with files appear under "Read your courses &
  guides" on the account page.

## 4d. Announcement bar

- Set the text and an optional **"Show until"** date in Admin → Settings. The
  bar renders on the home hero only while active (`active_announcement()` checks
  the expiry). Visitors can't dismiss it; a **"Remove announcement"** button in
  Settings clears the text and date in one click.

## 4e. Streaks & achievement badges

- **"I showed up today"** on the account page records a daily check-in
  (`User.check_in()`): a missed day resets the current streak, and the longest
  streak is remembered. No per-day table — just four columns on `users`.
- **Badges** are defined in `app/services/badges.py` (categories + tiers) and are
  derived live from stats, so they can never fall out of sync:
  - *Showing Up* (streak: 3/7/30/100/365 days)
  - *Storyteller* (posts: 1/10/25/50/100)
  - *Kindred Spirit* (likes earned on your comments: 5/25/50/100)
  - plus a special **Founder** badge for the owner.
- Art is **procedural SVG** (`partials/badges.html`, shared gradients in
  `partials/badge_defs.html`) — a hexagon shield that grows more ornate the
  higher the tier (rays → rank wings → ribbon → gold rim → gems), so tiers in a
  category share an emblem/colour but the higher one looks clearly evolved.
  Run `python scripts/badge_preview.py` to render the whole set to
  `instance/badge_preview.html`.
- Members pick **up to three** badges to feature (Settings → *Your badges*).
  Featured badges show on the public profile (`/u/<id>`) and the member's top
  badge shows next to their name in the community — hover any badge to see the
  milestone. Anonymous posts never reveal a badge.
- **Studio → Badges** shows every category's full tier ladder (rendered) and
  lets the owner retune each **milestone threshold** (values must climb per
  category). Overrides live in `Setting["_badge_thresholds"]`; a *Reset to
  defaults* button restores the originals. Titles/emblems/tier counts are fixed
  in code; only thresholds are editable, and phrases regenerate to match.

## 4f. "My Journey" keepsake (Creator export)

- Creator members (and the owner) can download a designed **PDF keepsake** of
  their journey at `/account/journey.pdf`: streak stats, the days they showed up,
  and every quote they've favorited, closed with a warm line — made to keep,
  print, or share. The gate is `is_premium()` → `user.is_creator()`.
- Built with **fpdf2** (`app/services/journey.py`) — pure Python, no system
  libraries, so it deploys anywhere. Core PDF fonts are Latin-1 only, so all
  user text is transliterated first (`_t`). The layout uses the brand palette
  and a painted horizon gradient.
- Real check-in history comes from the `check_ins` table (one row per day,
  written by `User.check_in()`); streak columns on `users` stay as the fast
  summary. `python scripts/journey_preview.py` renders a sample PDF (and PNG)
  to `instance/`.

## 4g. Memberships, videos & the home spotlight

- **Tiers** live on `users.membership` (`none` / `healing` / `creator`); the
  owner is always treated as a Creator.
- **Memberships sell on their own** (not as catalogue products). They're
  `MembershipPlan` rows edited in **Studio → Plans** (`/admin/memberships`): set a
  price, billing period, Lemon Squeezy **checkout URL** and **variant id**, then
  flip the plan **Live**. Visitors compare and buy them on the public
  **`/membership`** page, which shows a three-column plan grid and a full
  feature-comparison table.
- **Buy a membership**: when Lemon Squeezy reports a **paid** order whose variant
  matches a plan, the buyer's account is upgraded automatically; a **refund**
  recomputes the tier from their remaining paid membership orders (so it's
  revoked). Bought before making an account? The tier is granted at first login
  (matched by email). Logic lives in `app/services/memberships.py`, wired through
  `upsert_order` so both webhooks and the dashboard "Sync" stay consistent.
  `seed.py` creates the two plans (Healing, Creator) inactive — add a price and
  checkout link, then go Live.
- **Assign by hand**: the owner can still set any tier in **Studio → Members**
  (`/admin/members`); manual changes always win. That page also shows live counts
  and a spotlight pick-list of Creator members + their Instagram handles.
  - **Free**: shop, quotes, announcements, badges; can *peek* at the community
    (top 3 threads per forum, top 5 comments each) but can't post, reply or like.
  - **Healing**: full community read + post/reply/like, **profile links** (any
    URL), the **My Journey** export, a **marketplace** listing (one at a time),
    and can **browse** the Content Library (playback stays locked).
  - **Creator**: everything in Healing, plus **watching** the Content Library,
    **unlimited** marketplace listings, and eligibility for the home spotlight.
  - Gating helpers: `User.is_member()` (Healing+) and `User.is_creator()`.
- **Membership choice** is surfaced on the signup page and managed from
  **Settings** (change plan / cancel). Cancelling drops the tier to `none` and
  auto-hides the member's marketplace listings; billing itself is cancelled in
  Lemon Squeezy.
- **Video room** (`/watch`, Creator-only): the owner uploads videos in **Studio
  → Videos**. Files are **streamed to a directory on disk** in 1 MB chunks
  (`VIDEO_STORAGE_DIR`, a mounted persistent disk on Render — see `render.yaml`)
  so even large clips never load fully into worker memory; only the small 16:9
  thumbnail lives in the DB. Validated in `app/services/videos.py` and served
  with HTTP **Range** support (via `send_file`) so `<video>` can seek. The
  per-file cap is `MAX_VIDEO_MB` (default 1024 MB); oversized uploads show an
  inline error on the form rather than an error page. No download control is
  exposed. New videos surface as a nudge on the Creator's home page.
- **Profile links** are a members' perk (Healing+) and accept **any** URL. A
  label is optional; when blank we derive one from the URL. Up to
  `PROFILE_LINK_MAX` links, shown on the public profile.
- **Marketplace** (`app/main` routes + `app/services/listings.py`): two
  categories — *Digital products* and *Services* (services add a location).
  Listings carry title, description, price (free text), website URL, up to 5
  images (first = thumbnail, stored in the DB like avatars) and up to 12 tags.
  We never sell here; the CTA counts an outbound click and redirects to the
  seller's site. Tier caps (`MARKETPLACE_LIMITS`): Healing 1 active, Creator
  unlimited; `enforce_listing_limits` hides overflow when a tier drops. Studio →
  Marketplace lets the owner hide/restore/delete any listing.
- **Home spotlight** (Studio → Settings): *Creator of the Month* now shows the
  chosen creator's **photo, name, @handle and bio** (less dead space), and *Reel
  of the Week* — an Instagram reel URL is turned into an embedded iframe
  (`instagram_embed_url`) with a watch-on-Instagram link and an optional owner
  note. Each card has a bold gradient tag. CSP allows `www.instagram.com`.
- **Announcements**: a quick single one lives in Settings, and any number of
  extra ones (each with an optional expiry) can be added below it; they stack
  tidily in a strip at the very top of the home page (non-dismissible).
- **Subjects**: each course/guide can be filed under a subject; the catalogue
  shows a second row of filter tabs for the subjects actually in use.

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
