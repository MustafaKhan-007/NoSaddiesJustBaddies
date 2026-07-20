"""Public pages."""
import json
import logging
import os
import re
from datetime import date, datetime
from flask import (Response, abort, current_app, flash, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db, limiter
from sqlalchemy import func

from ..models import (MARKETPLACE_KINDS, MARKETPLACE_KIND_LABELS,
                      MARKETPLACE_TAG_MAX, MARKETPLACE_TAGS, PRODUCT_SUBJECTS,
                      CoachingRequest, ContactMessage, FaqItem,
                      ListingImage, MarketplaceListing, MembershipPlan, Order,
                      Page, Product, ProductAsset, Quote, QuoteFavorite,
                      ReelReview, ReelReviewApplication, Subscriber,
                      Testimonial, User, Video, utcnow)
from ..services import quotes as quotes_service
from ..services import reel_reviews as reel_svc
from ..services import settings as settings_service
from ..services.assets import docx_to_html
from ..services.avatars import AvatarError, process_avatar
from ..services.badges import CATEGORIES, category_progress, earned_badges
from ..services.checkout import with_custom, with_success_redirect
from ..services.journey import build_journey_pdf
from ..services.mailer import send_contact_notification
from ..services.recommend import INTENTS, recommend_products, valid_intent_keys
from ..services.listings import (ListingError, can_add_listing, listing_limit,
                                 process_listing_image)
from ..services.social import (ALLOWED_LABELS, clean_social_links,
                               instagram_embed_url, instagram_handle,
                               instagram_profile_url)
from ..services.videos import VideoError, process_video, process_video_bytes
from . import bp

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: how many custom profile links a member may add
PROFILE_LINK_MAX = 5


def _collect_profile_links(form):
    """Read paired label/url inputs (link_label_N / link_url_N) into a clean list."""
    links = []
    for i in range(PROFILE_LINK_MAX):
        url = (form.get(f"link_url_{i}") or "").strip()[:300]
        label = (form.get(f"link_label_{i}") or "").strip()[:40]
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not label:
            label = re.sub(r"^https?://(www\.)?", "", url).split("/")[0][:40]
        links.append({"label": label, "url": url})
    return links


def _valid_badge_choices(keys):
    """Keep up to 3 chosen badge categories the member has actually earned."""
    earned = {b["cat"] for b in earned_badges(current_user)}
    out = []
    for key in keys:
        if key in CATEGORIES and key in earned and key not in out:
            out.append(key)
        if len(out) >= 3:
            break
    return out

SEED_TESTIMONIALS = [
    {"quote": "I bought the notebook the week everything fell apart. It didn't fix my life \u2014 it gave me somewhere to stand while I fixed it.", "first_name": "Dana"},
    {"quote": "The course felt like a friend who's been through it, not a guru shouting at me. I finished it. I never finish things.", "first_name": "Priya"},
    {"quote": "Small daily pages. That's it. Three months later I barely recognize my mornings.", "first_name": "Leah"},
]


def _published_products():
    return Product.query.filter_by(status="published")


def _quote_context():
    """Everything the daily quote card needs."""
    today = date.today()
    quote = quotes_service.quote_for(today, count_view=True)
    ctx = {"today_quote": quote, "today": today, "quote_favorited": False}
    if current_user.is_authenticated and quote:
        ctx["quote_favorited"] = QuoteFavorite.query.filter_by(
            user_id=current_user.id, quote_id=quote.id).first() is not None
    return ctx


def _spotlight_context():
    """Creator of the Month + Reel of the Week, straight from site settings."""
    site = settings_service.all_settings()
    creator = None
    if (site.get("creator_name") or "").strip():
        raw_ig = (site.get("creator_instagram") or "").strip()
        handle = instagram_handle(raw_ig)
        profile = instagram_profile_url(handle) if handle else ""
        creator = {
            "name": site["creator_name"].strip(),
            "instagram": profile or raw_ig,
            "handle": handle,
            "blurb": (site.get("creator_blurb") or "").strip(),
        }
    reel = None
    reel_url = (site.get("reel_url") or "").strip()
    if reel_url:
        reel = {
            "url": reel_url,
            "embed": instagram_embed_url(reel_url),
            "description": (site.get("reel_description") or "").strip(),
        }
    return {"creator_of_month": creator, "reel_of_week": reel}


def _video_notice():
    """Newest published video, only for the first day after it goes live."""
    if not (getattr(current_user, "is_authenticated", False) and current_user.is_creator()):
        return None
    video = (Video.query.filter_by(published=True)
             .order_by(Video.sort_order, Video.created_at.desc()).first())
    if video is None or not video.created_at:
        return None
    # hide after ~24 hours
    age = utcnow() - video.created_at
    if age.total_seconds() > 24 * 3600:
        return None
    return video


@bp.route("/")
def index():
    featured = (_published_products().filter_by(featured=True)
                .order_by(Product.sort_order, Product.created_at.desc()).limit(3).all())
    testimonials = (Testimonial.query.filter_by(show_on_home=True)
                    .order_by(Testimonial.sort_order).limit(3).all())
    return render_template(
        "main/index.html",
        featured=featured,
        testimonials=testimonials or SEED_TESTIMONIALS,
        testimonials_are_models=bool(testimonials),
        latest_video=_video_notice(),
        **_spotlight_context(),
        **_quote_context(),
    )


@bp.route("/courses")
def courses():
    ptype = request.args.get("type", "all")
    query = _published_products()
    if ptype == "course":
        query = query.filter_by(type="course")
    elif ptype == "guide":
        query = query.filter_by(type="guide")
    else:
        ptype = "all"

    # subjects that actually have published products, in the canonical order
    used = {s for (s,) in db.session.query(Product.subject)
            .filter(Product.status == "published", Product.subject.isnot(None))
            .distinct().all() if s}
    subjects = [s for s in PRODUCT_SUBJECTS if s in used]

    active_subject = request.args.get("subject")
    if active_subject and active_subject in PRODUCT_SUBJECTS:
        query = query.filter(Product.subject == active_subject)
    else:
        active_subject = None

    products = query.order_by(Product.sort_order, Product.created_at.desc()).all()
    return render_template("main/courses.html", products=products, active_type=ptype,
                           subjects=subjects, active_subject=active_subject)


#: the comparison matrix shown on /membership. Each row: (label, free, healing, creator)
#: values are True (check), False (blank) or a short string (note).
MEMBERSHIP_MATRIX = [
    ("Buy courses & guides", True, True, True),
    ("Daily quotes & motivation", True, True, True),
    ("Earn & display badges", True, True, True),
    ("Read the community", "Top 3 threads", True, True),
    ("Post, reply & like", False, True, True),
    ("Browse the Content Hub", False, True, True),
    ("Watch Content Hub videos", False, False, True),
    ("Request a weekly reel review", False, False, True),
    ("1-on-1 coaching sessions", False, False, True),
    ("Profile links", False, True, True),
    ("My Journey keepsake export", False, True, True),
    ("Showcase listings", False, "1 active", "Unlimited"),
    ("Home-page spotlight eligibility", False, False, True),
]


def _checkout_url(url):
    """Send buyers back to My space after Lemon checkout."""
    out = url or ""
    try:
        success = url_for("main.account", _external=True)
        out = with_success_redirect(out, success)
    except RuntimeError:
        pass  # no request context (e.g. some tests)
    return out


@bp.route("/membership")
def membership():
    plans = {p.tier: p for p in MembershipPlan.query.filter_by(active=True).all()}
    current = (current_user.effective_membership()
               if current_user.is_authenticated else None)
    checkout = {}
    for tier, plan in plans.items():
        checkout[tier] = _checkout_url(plan.ls_checkout_url) if plan else None
    return render_template("main/membership.html", plans=plans,
                           matrix=MEMBERSHIP_MATRIX, current=current,
                           checkout=checkout)


# --- marketplace (member adverts; we redirect out, we don't sell) ----------

MARKETPLACE_SORTS = {"popular": "Most popular", "new": "Newest"}


def _showcase_index():
    kind = request.args.get("kind")
    if kind not in MARKETPLACE_KINDS:
        kind = None
    q = (request.args.get("q") or "").strip()
    tag = (request.args.get("tag") or "").strip()
    location = (request.args.get("location") or "").strip()
    sort = request.args.get("sort", "popular")
    view = "list" if request.args.get("view") == "list" else "tiles"

    base = MarketplaceListing.query.filter_by(active=True)
    if kind:
        base = base.filter_by(kind=kind)
    query = base
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(MarketplaceListing.title.ilike(like),
                                    MarketplaceListing.description.ilike(like)))
    if location:
        query = query.filter(MarketplaceListing.location.ilike(f"%{location}%"))
    if sort == "new":
        query = query.order_by(MarketplaceListing.created_at.desc())
    else:
        sort = "popular"
        query = query.order_by(MarketplaceListing.clicks.desc(),
                               MarketplaceListing.created_at.desc())
    listings = query.all()
    if tag:
        listings = [ln for ln in listings if tag in ln.tags()]

    # curated catalogue first, then any custom tags already in use
    used = {t for ln in base.all() for t in ln.tags()}
    all_tags = list(MARKETPLACE_TAGS) + sorted(used - set(MARKETPLACE_TAGS))
    # distinct locations from active service listings (for chip filters)
    loc_q = MarketplaceListing.query.filter_by(active=True, kind="service")
    locations = sorted({
        (ln.location or "").strip() for ln in loc_q.all() if (ln.location or "").strip()
    }, key=str.lower)
    return render_template("marketplace/index.html", listings=listings,
                           kind=kind, kinds=MARKETPLACE_KIND_LABELS, q=q, tag=tag,
                           location=location, sort=sort, sorts=MARKETPLACE_SORTS,
                           view=view, all_tags=all_tags, locations=locations)


@bp.route("/showcase")
def showcase():
    return _showcase_index()


@bp.route("/marketplace")
def marketplace():
    return _showcase_index()


@bp.route("/marketplace/l/<int:listing_id>")
def listing_detail(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id)
    if ln is None or not ln.active:
        abort(404)
    return render_template("marketplace/detail.html", listing=ln)


@bp.route("/marketplace/image/<int:image_id>")
def listing_image(image_id):
    img = db.session.get(ListingImage, image_id)
    if img is None:
        abort(404)
    resp = Response(bytes(img.data), mimetype=img.mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/marketplace/go/<int:listing_id>")
def listing_go(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id)
    if ln is None or not ln.active:
        abort(404)
    url = ln.website_url or ""
    if not url.lower().startswith(("http://", "https://")):
        abort(404)
    ln.clicks = (ln.clicks or 0) + 1
    db.session.commit()
    return redirect(url)


@bp.route("/marketplace/mine")
@login_required
def my_listings():
    if not current_user.is_member():
        flash("The marketplace is a members' perk \u2014 join to advertise your "
              "products and services.", "info")
        return redirect(url_for("main.membership"))
    mine = (MarketplaceListing.query.filter_by(user_id=current_user.id)
            .order_by(MarketplaceListing.active.desc(),
                      MarketplaceListing.created_at.desc()).all())
    return render_template("marketplace/mine.html", listings=mine,
                           limit=listing_limit(current_user),
                           can_add=can_add_listing(current_user))


_TAG_LOOKUP = {t.lower(): t for t in MARKETPLACE_TAGS}


def _collect_listing_tags(form):
    """Pick tags from the checklist + optional custom comma list (capped)."""
    seen, out = set(), []
    for raw in form.getlist("tags"):
        key = (raw or "").strip().lower()
        if not key or key in seen:
            continue
        # prefer the curated spelling when it matches
        label = _TAG_LOOKUP.get(key) or (raw or "").strip()[:40]
        if not label:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= MARKETPLACE_TAG_MAX:
            return out
    for part in (form.get("tags_custom") or "").replace("\n", ",").split(","):
        t = part.strip()[:40]
        key = t.lower()
        if not t or key in seen:
            continue
        seen.add(key)
        out.append(_TAG_LOOKUP.get(key, t))
        if len(out) >= MARKETPLACE_TAG_MAX:
            break
    return out


@bp.route("/marketplace/new", methods=["GET", "POST"])
@bp.route("/marketplace/<int:listing_id>/edit", methods=["GET", "POST"])
@login_required
def listing_form(listing_id=None):
    if not current_user.is_member():
        flash("The marketplace is a members' perk \u2014 join to advertise here.", "info")
        return redirect(url_for("main.membership"))

    listing = None
    if listing_id:
        listing = db.session.get(MarketplaceListing, listing_id)
        if listing is None or listing.user_id != current_user.id:
            abort(404)

    if request.method == "POST":
        kind = request.form.get("kind")
        if kind not in MARKETPLACE_KINDS:
            kind = "product"
        title = (request.form.get("title") or "").strip()[:140]
        description = (request.form.get("description") or "").strip()
        website = (request.form.get("website_url") or "").strip()[:500]
        price = (request.form.get("price") or "").strip()[:80] or None
        location = (request.form.get("location") or "").strip()[:120] or None
        tags = _collect_listing_tags(request.form)

        errors = []
        if not title:
            errors.append("Give your listing a title.")
        if not website.lower().startswith(("http://", "https://")):
            if website and not website.startswith(("http://", "https://")):
                website = "https://" + website
            if not website:
                errors.append("Add the link where people can find it.")
        if kind == "product":
            location = None
        elif kind == "service" and not location:
            errors.append("Add a location for your service (city, region, or Remote).")

        # tier limit only matters when creating (or reactivating) a live listing
        if listing is None and not can_add_listing(current_user):
            lim = listing_limit(current_user)
            errors.append(
                f"Your plan allows {lim} active listing{'s' if lim != 1 else ''}. "
                "Upgrade to Creator for unlimited, or remove one first.")

        new_images = []
        if not errors:
            for f in request.files.getlist("images"):
                if f and f.filename:
                    try:
                        new_images.append(process_listing_image(f))
                    except ListingError as exc:
                        errors.append(str(exc))

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            if listing is None:
                listing = MarketplaceListing(user_id=current_user.id)
                db.session.add(listing)
            listing.kind = kind
            listing.title = title
            listing.description = description
            listing.website_url = website
            listing.price = price
            listing.location = location
            listing.set_tags(tags)
            for img_id in request.form.getlist("remove_image"):
                img = db.session.get(ListingImage, int(img_id)) if img_id.isdigit() else None
                if img and img.listing_id == listing.id:
                    db.session.delete(img)
            start = len(listing.images)
            for i, (data, mime) in enumerate(new_images):
                listing.images.append(ListingImage(data=data, mime=mime, sort_order=start + i))
            db.session.commit()
            flash("Listing saved. It's live in the marketplace.", "success")
            return redirect(url_for("main.my_listings"))

    chosen = set(listing.tags()) if listing else set()
    # keep any custom tags the listing already has, even if not in the catalogue
    custom_existing = [t for t in chosen if t not in MARKETPLACE_TAGS]
    return render_template("marketplace/form.html", listing=listing,
                           kinds=MARKETPLACE_KIND_LABELS,
                           tag_catalog=MARKETPLACE_TAGS,
                           tag_max=MARKETPLACE_TAG_MAX,
                           chosen_tags=chosen,
                           tags_custom=", ".join(custom_existing))


@bp.route("/marketplace/<int:listing_id>/delete", methods=["POST"])
@login_required
def listing_delete(listing_id):
    listing = db.session.get(MarketplaceListing, listing_id)
    if listing is None or listing.user_id != current_user.id:
        abort(404)
    db.session.delete(listing)
    db.session.commit()
    flash("Listing removed.", "success")
    return redirect(url_for("main.my_listings"))


@bp.route("/courses/<slug>")
def product_detail(slug):
    product = Product.query.filter_by(slug=slug, status="published").first_or_404()
    curriculum = []
    if product.curriculum_json:
        try:
            curriculum = json.loads(product.curriculum_json)
        except ValueError:
            curriculum = []
    contents = [line.strip() for line in (product.contents_text or "").splitlines() if line.strip()]
    related = (_published_products()
               .filter(Product.type == product.type, Product.id != product.id)
               .order_by(Product.sort_order).limit(3).all())
    testimonials = product.testimonials.order_by(Testimonial.sort_order).all()
    faqs = FaqItem.query.order_by(FaqItem.sort_order).limit(6).all()
    checkout_url = _checkout_url(product.ls_checkout_url) if product.ls_checkout_url else ""
    already_owned = _owns_product(current_user, product) if current_user.is_authenticated else False
    return render_template("main/product_detail.html", product=product,
                           curriculum=curriculum, contents=contents,
                           related=related, testimonials=testimonials, faqs=faqs,
                           checkout_url=checkout_url, already_owned=already_owned)


@bp.route("/courses/<slug>/gift", methods=["POST"])
def gift_checkout(slug):
    """Send the buyer to checkout with a friend's account tagged as recipient.

    The friend must already have an account; the gift email rides along as
    Lemon Squeezy custom data and the webhook grants them access on payment.
    """
    product = Product.query.filter_by(slug=slug, status="published").first_or_404()
    friend = (request.form.get("gift_email") or "").strip().lower()
    if not friend:
        flash("Add your friend's account email to gift this.", "error")
        return redirect(url_for("main.product_detail", slug=slug))
    recipient = (User.query
                 .filter(func.lower(User.email) == friend, User.deleted_at.is_(None))
                 .first())
    if recipient is None:
        flash("We couldn't find an account with that email. Ask your friend to "
              "sign up first, then gift away.", "error")
        return redirect(url_for("main.product_detail", slug=slug))
    if not (product.ls_checkout_url or "").strip():
        flash("This one isn't available for checkout yet.", "error")
        return redirect(url_for("main.product_detail", slug=slug))
    url = with_custom(_checkout_url(product.ls_checkout_url), gift_to=friend)
    return redirect(url)


@bp.route("/about")
def about():
    page = Page.query.filter_by(slug="about").first()
    return render_template("main/about.html", page=page)


@bp.route("/quotes")
def quotes():
    today = date.today()
    # Visitors see only today's quote. The archive is a member perk, and it
    # only goes back as far as the day their account was created.
    if not current_user.is_authenticated:
        q = quotes_service.quote_for(today)
        recent = [(today, q)] if q else []
        return render_template("main/quotes.html", recent=recent, today=today,
                               favorite_ids=set())

    created = (current_user.created_at.date()
               if current_user.created_at else today)
    days = max(1, min((today - created).days + 1, 366))
    recent = quotes_service.recent_quotes(days, today=today)
    favorite_ids = {f.quote_id for f in current_user.favorites}
    return render_template("main/quotes.html", recent=recent, today=today,
                           favorite_ids=favorite_ids)


@bp.route("/quotes/<int:quote_id>/favorite", methods=["POST"])
@login_required
def toggle_favorite(quote_id):
    quote = db.session.get(Quote, quote_id) or abort(404)
    existing = QuoteFavorite.query.filter_by(user_id=current_user.id, quote_id=quote.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(QuoteFavorite(user_id=current_user.id, quote_id=quote.id))
    db.session.commit()
    return redirect(request.form.get("next") or url_for("main.quotes"))


@bp.route("/account")
@login_required
def account():
    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    orders = (Order.query.filter_by(buyer_email=current_user.email)
              .order_by(Order.created_at.desc()).all())
    favorites = (db.session.query(Quote).join(QuoteFavorite)
                 .filter(QuoteFavorite.user_id == current_user.id)
                 .order_by(QuoteFavorite.created_at.desc()).all())
    coaching_url = settings_service.get_setting("coaching_checkout_url") or ""
    coaching_checkout = _checkout_url(coaching_url) if coaching_url else ""
    my_coaching = (CoachingRequest.query.filter_by(user_id=current_user.id)
                   .order_by(CoachingRequest.created_at.desc()).limit(5).all())
    owner_coaching = []
    if current_user.is_admin:
        owner_coaching = (CoachingRequest.query
                          .filter(CoachingRequest.status == "pending")
                          .order_by(CoachingRequest.created_at.desc())
                          .limit(20).all())
    return render_template("main/account.html", greeting=greeting, orders=orders,
                           favorites=favorites, library=_owned_products(current_user),
                           recommended=recommend_products(current_user),
                           premium=is_premium(current_user),
                           coaching_checkout=coaching_checkout,
                           my_coaching=my_coaching,
                           owner_coaching=owner_coaching)


@bp.route("/account/coaching", methods=["POST"])
@login_required
def request_coaching():
    if not current_user.is_creator():
        flash("1-on-1 coaching is a Creator membership perk.", "info")
        return redirect(url_for("main.membership"))
    message = (request.form.get("message") or "").strip()[:2000]
    preferred = (request.form.get("preferred_times") or "").strip()[:300]
    if len(message) < 10:
        flash("Tell us a little about what you'd like help with (a sentence or two).",
              "error")
        return redirect(url_for("main.account") + "#coaching")
    if not preferred:
        flash("Pick a date and time for your session.", "error")
        return redirect(url_for("main.account") + "#coaching")
    # datetime-local is "YYYY-MM-DDTHH:MM" — store a readable version
    try:
        when = datetime.fromisoformat(preferred)
        preferred_display = when.strftime("%a %b %d, %Y at %I:%M %p").replace(" 0", " ")
    except ValueError:
        preferred_display = preferred
    db.session.add(CoachingRequest(
        user_id=current_user.id, message=message,
        preferred_times=preferred_display[:300]))
    db.session.commit()
    flash("Coaching request sent — check out below to book your $100 session.",
          "success")
    checkout = settings_service.get_setting("coaching_checkout_url") or ""
    if checkout:
        return redirect(_checkout_url(checkout))
    return redirect(url_for("main.account") + "#coaching")


@bp.route("/account/journey.pdf")
@login_required
def journey_pdf():
    if not is_premium(current_user):
        flash("The My Journey keepsake is a little something for members who've "
              "joined a course or guide. It's waiting for you when you are.", "info")
        return redirect(url_for("main.account"))
    pdf_bytes = build_journey_pdf(current_user)
    stamp = date.today().isoformat()
    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="my-journey-{stamp}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/account/settings")
@login_required
def settings():
    return render_template("main/settings.html", intents=INTENTS,
                           user_goals=set(current_user.goals()),
                           links=current_user.links(),
                           link_max=PROFILE_LINK_MAX,
                           can_link=current_user.is_member(),
                           badge_progress=category_progress(current_user),
                           chosen_badges=set(current_user.displayed_badges()))


@bp.route("/account/membership/cancel", methods=["POST"])
@login_required
def cancel_membership():
    if current_user.is_admin:
        flash("The owner account always keeps Creator access.", "info")
        return redirect(url_for("main.settings"))
    if current_user.membership == "none":
        flash("You're on the free plan already.", "info")
        return redirect(url_for("main.settings"))
    current_user.membership = "none"
    from ..services.listings import enforce_listing_limits
    enforce_listing_limits(current_user)
    db.session.commit()
    flash("Your membership is cancelled. If you were billed through Lemon Squeezy, "
          "also cancel the subscription there so you're not charged again.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/account/checkin", methods=["POST"])
@login_required
def checkin():
    if current_user.check_in():
        db.session.commit()
        flash("You showed up today. That's the whole thing.", "success")
    else:
        flash("Already checked in today \u2014 see you tomorrow.", "info")
    return redirect(request.form.get("next") or url_for("main.account"))


@bp.route("/u/<int:user_id>")
def profile(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        abort(404)
    return render_template("main/profile.html", profile_user=user)


@bp.route("/account/profile", methods=["POST"])
@login_required
def update_profile():
    name = (request.form.get("display_name") or "").strip()[:80]
    bio = (request.form.get("bio") or "").strip()[:400]
    current_user.display_name = name or None
    current_user.bio = bio or None
    current_user.default_anonymous = request.form.get("default_anonymous") == "1"
    current_user.set_goals(valid_intent_keys(request.form.getlist("goals")))
    # profile links are a members' perk (Healing+); any link is allowed
    if current_user.is_member():
        current_user.set_links(_collect_profile_links(request.form))
    current_user.set_displayed_badges(_valid_badge_choices(request.form.getlist("badges_display")))

    if request.form.get("remove_avatar") == "1":
        current_user.avatar_data = None
        current_user.avatar_mime = None
        current_user.avatar_url = None

    upload = request.files.get("avatar_file")
    if upload and upload.filename:
        try:
            data, mime = process_avatar(upload)
            current_user.avatar_data = data
            current_user.avatar_mime = mime
            current_user.avatar_url = None
        except AvatarError as exc:
            db.session.commit()  # keep the other field edits
            flash(str(exc), "error")
            return redirect(url_for("main.settings"))

    db.session.commit()
    flash("Saved. Nice to meet you properly.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/avatar/<int:user_id>")
def avatar(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.avatar_data:
        abort(404)
    resp = Response(bytes(user.avatar_data), mimetype=user.avatar_mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# --- library: read purchased courses & guides online -----------------------

def _owns_product(user, product) -> bool:
    """True if the user may read a product's files (the owner, or a buyer)."""
    if not user.is_authenticated:
        return False
    if user.is_admin:
        return True
    email = (user.email or "").lower()
    return db.session.query(Order.id).filter(
        Order.product_id == product.id,
        Order.status == "paid",
        db.or_(func.lower(Order.buyer_email) == email,
               func.lower(Order.gift_to_email) == email),
    ).first() is not None


def _owned_products(user):
    """Distinct products the user has bought (or been gifted) — shown in My space."""
    if not user.is_authenticated:
        return []
    email = (user.email or "").lower()
    return (Product.query.join(Order, Order.product_id == Product.id)
            .filter(Order.status == "paid",
                    db.or_(func.lower(Order.buyer_email) == email,
                           func.lower(Order.gift_to_email) == email))
            .order_by(Product.title).distinct().all())


def is_premium(user) -> bool:
    """My Journey + profile links are a members' perk (Healing/Creator or owner)."""
    return bool(getattr(user, "is_authenticated", False) and user.is_member())


@bp.route("/library/<slug>")
@login_required
def library_item(slug):
    product = Product.query.filter_by(slug=slug).first_or_404()
    if not _owns_product(current_user, product):
        abort(404)   # hide existence from non-buyers
    contents = [line.strip() for line in (product.contents_text or "").splitlines()
                if line.strip()]
    curriculum = []
    if product.curriculum_json:
        try:
            curriculum = json.loads(product.curriculum_json)
        except ValueError:
            curriculum = []
    readable = []
    for asset in product.assets:
        entry = {"asset": asset}
        if asset.kind == "docx":
            entry["html"] = docx_to_html(bytes(asset.data))
        readable.append(entry)
    return render_template("main/library_item.html", product=product,
                           readable=readable, contents=contents, curriculum=curriculum)


@bp.route("/library/<slug>/file/<int:asset_id>")
@login_required
def library_asset(slug, asset_id):
    product = Product.query.filter_by(slug=slug).first_or_404()
    if not _owns_product(current_user, product):
        abort(404)
    asset = db.session.get(ProductAsset, asset_id)
    if asset is None or asset.product_id != product.id:
        abort(404)
    resp = Response(bytes(asset.data), mimetype=asset.mime)
    # inline (viewer), never an attachment; don't let it linger in shared caches
    resp.headers["Content-Disposition"] = f'inline; filename="{asset.filename}"'
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# --- Content Library --------------------------------------------------------
# Members (Healing+) can browse titles/thumbnails; only Creators can play.

def _can_play_videos(user) -> bool:
    """Creator members and the site owner can press play."""
    return bool(getattr(user, "is_authenticated", False)
                and (getattr(user, "is_admin", False) or user.is_creator()))


def _video_playable(video) -> bool:
    """True when the file (disk or legacy DB bytes) is actually available."""
    if video is None:
        return False
    if video.data:
        return True
    if video.disk_name:
        path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"], video.disk_name)
        return os.path.exists(path)
    return False


@bp.route("/watch")
def videos():
    """Content Hub: public reel reviews + member video library."""
    can_browse = (current_user.is_authenticated and current_user.is_member())
    can_play = _can_play_videos(current_user)
    items = []
    if can_browse:
        items = (Video.query.filter_by(published=True)
                 .order_by(Video.sort_order, Video.created_at.desc()).all())
    reviews = (ReelReview.query.filter_by(published=True)
               .order_by(ReelReview.created_at.desc()).limit(24).all())
    my_app = None
    week_key = reel_svc.current_week_key()
    if current_user.is_authenticated and current_user.is_creator():
        my_app = reel_svc.application_for(current_user.id, week_key)
    return render_template(
        "main/videos.html", videos=items, can_browse=can_browse, can_play=can_play,
        reviews=reviews, my_application=my_app, week_key=week_key,
        max_mb=current_app.config.get("REEL_RAW_MAX_MB", 100),
    )


@bp.route("/watch/review-request", methods=["POST"])
@login_required
def reel_review_request():
    if not current_user.is_creator():
        flash("Reel reviews are a Creator membership perk.", "info")
        return redirect(url_for("main.membership"))
    week = reel_svc.current_week_key()
    if reel_svc.application_for(current_user.id, week):
        flash("You've already entered this week's reel-review draw. "
              "A fresh round opens every Monday.", "info")
        return redirect(url_for("main.videos") + "#reviews")
    reel_url = (request.form.get("reel_url") or "").strip()[:500]
    if not reel_svc.is_instagram_reel_url(reel_url):
        flash("Paste the Instagram link of the reel you posted "
              "(it should look like instagram.com/reel/\u2026).", "error")
        return redirect(url_for("main.videos") + "#reviews")
    upload = request.files.get("raw_video")
    if not upload or not upload.filename:
        flash("Upload the raw video file for your reel too.", "error")
        return redirect(url_for("main.videos") + "#reviews")
    # Store in the database so the owner can download after deploys
    # (Render's local disk is wiped unless a persistent volume is attached).
    max_bytes = current_app.config.get("REEL_RAW_MAX_MB", 100) * 1024 * 1024
    try:
        mime, fname, size, data = process_video_bytes(upload, max_bytes)
    except VideoError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.videos") + "#reviews")
    app_row = ReelReviewApplication(
        user_id=current_user.id, week_key=week, reel_url=reel_url,
        data=data, filename=fname, mime=mime, size=size)
    db.session.add(app_row)
    db.session.commit()
    flash("You're in this week's reel-review draw. One applicant is chosen at random.",
          "success")
    return redirect(url_for("main.videos") + "#reviews")


@bp.route("/watch/<int:video_id>")
@login_required
def watch(video_id):
    if not current_user.is_member():
        flash("The Content Hub videos are a members' perk \u2014 join to watch.", "info")
        return redirect(url_for("main.membership"))
    video = db.session.get(Video, video_id)
    # Owner can preview unpublished drafts; everyone else needs published.
    if video is None:
        abort(404)
    if not video.published and not current_user.is_admin:
        abort(404)
    more = (Video.query.filter(Video.published.is_(True), Video.id != video.id)
            .order_by(Video.sort_order, Video.created_at.desc()).limit(6).all())
    can_play = _can_play_videos(current_user)
    playable = _video_playable(video)
    return render_template("main/watch.html", video=video, more=more,
                           can_play=can_play, playable=playable)


@bp.route("/watch/<int:video_id>/thumb")
@login_required
def video_thumb(video_id):
    if not (current_user.is_authenticated and current_user.is_member()):
        abort(404)
    video = db.session.get(Video, video_id)
    if video is None or not video.thumb_data:
        abort(404)
    if not video.published and not current_user.is_admin:
        abort(404)
    resp = Response(bytes(video.thumb_data), mimetype=video.thumb_mime or "image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _range_response(data, mime, filename):
    """Serve bytes with HTTP Range support so <video> can seek."""
    length = len(data)
    range_header = request.headers.get("Range")
    if not range_header:
        resp = Response(data, mimetype=mime)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(length)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    start, end = 0, length - 1
    if m:
        if m.group(1):
            start = int(m.group(1))
        if m.group(2):
            end = int(m.group(2))
    start = max(0, start)
    end = min(end, length - 1)
    if start > end:
        resp = Response(status=416)
        resp.headers["Content-Range"] = f"bytes */{length}"
        return resp
    chunk = data[start:end + 1]
    resp = Response(chunk, status=206, mimetype=mime)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(len(chunk))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/watch/<int:video_id>/stream")
@login_required
def video_stream(video_id):
    if not _can_play_videos(current_user):
        abort(404)
    video = db.session.get(Video, video_id)
    if video is None:
        abort(404)
    if not video.published and not current_user.is_admin:
        abort(404)
    if video.disk_name:
        path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"], video.disk_name)
        if not os.path.exists(path):
            log.error("Video %s missing on disk: %s", video_id, path)
            abort(404)
        # conditional=True enables HTTP Range (206) so <video> can seek.
        resp = send_file(path, mimetype=video.mime or "video/mp4",
                         conditional=True, download_name=video.filename or "video",
                         as_attachment=False)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    if video.data:  # legacy rows still stored in the database
        return _range_response(bytes(video.data), video.mime or "video/mp4",
                               video.filename or "video")
    abort(404)


@bp.route("/watch/reviews/<int:review_id>/stream")
@login_required
def reel_review_stream(review_id):
    """Stream the owner's published review video (public to signed-in visitors)."""
    review = db.session.get(ReelReview, review_id)
    if review is None or not review.published or not review.review_disk_name:
        abort(404)
    path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"], review.review_disk_name)
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype=review.review_mime or "video/mp4",
                     conditional=False, download_name=review.review_filename or "review",
                     as_attachment=False)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template("main/password.html")
    current = (request.form.get("current_password") or "").strip()
    new = (request.form.get("new_password") or "").strip()
    if not current_user.check_password(current):
        flash("Your current password didn't match \u2014 no changes made.", "error")
        return redirect(url_for("main.change_password"))
    if len(new) < 8:
        flash("Your new password needs at least 8 characters.", "error")
        return redirect(url_for("main.change_password"))
    current_user.set_password(new)
    db.session.commit()
    flash("Password updated.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    if request.form.get("confirm") != "yes":
        flash("Account not deleted \u2014 the confirmation box wasn't ticked.", "error")
        return redirect(url_for("main.account"))
    current_user.deleted_at = utcnow()
    db.session.commit()
    from flask_login import logout_user
    logout_user()
    flash("Your account is closed. Thank you for the time you spent here.", "success")
    return redirect(url_for("main.index"))


@bp.route("/subscribe", methods=["POST"])
@limiter.limit("5 per minute")
def subscribe():
    email = (request.form.get("email") or "").strip().lower()
    if request.form.get("website"):  # honeypot
        return redirect(url_for("main.index"))
    if not EMAIL_RE.match(email) or len(email) > 255:
        flash("That doesn't look like an email address \u2014 mind checking it?", "subscribe-error")
        return redirect(url_for("main.index") + "#letter")
    if Subscriber.query.filter_by(email=email).first():
        flash("You're already in \u2014 see you Sunday.", "subscribe-success")
    else:
        db.session.add(Subscriber(email=email))
        db.session.commit()
        flash("You're in. One small step, every Sunday.", "subscribe-success")
    return redirect(url_for("main.index") + "#letter")


@bp.route("/faq")
def faq():
    items = FaqItem.query.order_by(FaqItem.sort_order).all()
    return render_template("main/faq.html", items=items)


@bp.route("/contact", methods=["GET", "POST"])
@limiter.limit("3 per hour", methods=["POST"])
def contact():
    if request.method == "POST":
        if request.form.get("website"):  # honeypot
            return redirect(url_for("main.contact"))
        name = (request.form.get("name") or "").strip()[:120]
        email = (request.form.get("email") or "").strip().lower()
        body = (request.form.get("message") or "").strip()[:5000]
        if not name or not body or not EMAIL_RE.match(email):
            flash("Please fill in your name, a valid email, and a message.", "error")
            return render_template("main/contact.html", form=request.form), 400
        db.session.add(ContactMessage(name=name, email=email, body=body))
        db.session.commit()
        send_contact_notification(name, email, body)
        flash("Got it. I read everything \u2014 you'll hear back soon.", "success")
        return redirect(url_for("main.contact"))
    return render_template("main/contact.html", form={})


@bp.route("/privacy")
def privacy():
    return _legal_page("privacy", "Privacy Policy")


@bp.route("/terms")
def terms():
    return _legal_page("terms", "Terms of Service")


@bp.route("/refunds")
def refunds():
    return _legal_page("refunds", "Refund Policy")


def _legal_page(slug, title):
    page = Page.query.filter_by(slug=slug).first()
    return render_template("main/page.html", page=page, fallback_title=title)
