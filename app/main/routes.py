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

from ..models import (PRODUCT_SUBJECTS, ContactMessage, FaqItem, MembershipPlan,
                      Order, Page, Product, ProductAsset, Quote, QuoteFavorite,
                      Subscriber, Testimonial, User, Video, utcnow)
from ..services import quotes as quotes_service
from ..services import settings as settings_service
from ..services.assets import docx_to_html
from ..services.avatars import AvatarError, process_avatar
from ..services.badges import CATEGORIES, category_progress, earned_badges
from ..services.journey import build_journey_pdf
from ..services.mailer import send_contact_notification
from ..services.recommend import INTENTS, recommend_products, valid_intent_keys
from ..services.social import (ALLOWED_LABELS, clean_social_links,
                               instagram_embed_url)
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
        creator = {
            "name": site["creator_name"].strip(),
            "instagram": (site.get("creator_instagram") or "").strip(),
            "image": (site.get("creator_image_url") or "").strip(),
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
    """The newest published video — creator members see a home-page nudge."""
    if not (getattr(current_user, "is_authenticated", False) and current_user.is_creator()):
        return None
    return (Video.query.filter_by(published=True)
            .order_by(Video.sort_order, Video.created_at.desc()).first())


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
    ("The owner's video room", False, False, True),
    ("Social links on your profile", False, False, True),
    ("Home-page spotlight eligibility", False, False, True),
    ("\u201cMy Journey\u201d keepsake export", False, False, True),
]


@bp.route("/membership")
def membership():
    plans = {p.tier: p for p in MembershipPlan.query.filter_by(active=True).all()}
    current = current_user.membership if current_user.is_authenticated else None
    return render_template("main/membership.html", plans=plans,
                           matrix=MEMBERSHIP_MATRIX, current=current)


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
    return render_template("main/product_detail.html", product=product,
                           curriculum=curriculum, contents=contents,
                           related=related, testimonials=testimonials, faqs=faqs)


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
    return render_template("main/account.html", greeting=greeting, orders=orders,
                           favorites=favorites, library=_owned_products(current_user),
                           recommended=recommend_products(current_user),
                           premium=is_premium(current_user))


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
                           can_link=current_user.is_creator(),
                           allowed_platforms=ALLOWED_LABELS,
                           badge_progress=category_progress(current_user),
                           chosen_badges=set(current_user.displayed_badges()))


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
    # profile links are a Creator perk, and only social platforms are allowed
    if current_user.is_creator():
        current_user.set_links(clean_social_links(_collect_profile_links(request.form)))
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
    return db.session.query(Order.id).filter(
        Order.product_id == product.id,
        Order.status == "paid",
        func.lower(Order.buyer_email) == (user.email or "").lower(),
    ).first() is not None


def _owned_products(user):
    """Distinct products the user has bought that have readable files."""
    if not user.is_authenticated:
        return []
    products = (Product.query.join(Order, Order.product_id == Product.id)
                .filter(Order.status == "paid",
                        func.lower(Order.buyer_email) == (user.email or "").lower())
                .order_by(Product.title).distinct().all())
    return [p for p in products if p.has_assets()]


def is_premium(user) -> bool:
    """My Journey + profile links are a Creator-membership perk (or owner)."""
    return bool(getattr(user, "is_authenticated", False) and user.is_creator())


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


# --- videos (Creator-membership perk) --------------------------------------

def _require_creator():
    """Flash + redirect if the current user isn't a Creator member; else None."""
    if not (getattr(current_user, "is_authenticated", False) and current_user.is_creator()):
        flash("The video room is a Creator-membership perk \u2014 it's waiting when "
              "you're ready.", "info")
        return redirect(url_for("main.courses"))
    return None


@bp.route("/watch")
@login_required
def videos():
    guard = _require_creator()
    if guard:
        return guard
    items = (Video.query.filter_by(published=True)
             .order_by(Video.sort_order, Video.created_at.desc()).all())
    return render_template("main/videos.html", videos=items)


@bp.route("/watch/<int:video_id>")
@login_required
def watch(video_id):
    guard = _require_creator()
    if guard:
        return guard
    video = db.session.get(Video, video_id)
    if video is None or not video.published:
        abort(404)
    more = (Video.query.filter(Video.published.is_(True), Video.id != video.id)
            .order_by(Video.sort_order, Video.created_at.desc()).limit(6).all())
    return render_template("main/watch.html", video=video, more=more)


@bp.route("/watch/<int:video_id>/thumb")
@login_required
def video_thumb(video_id):
    if not (current_user.is_authenticated and current_user.is_creator()):
        abort(404)
    video = db.session.get(Video, video_id)
    if video is None or not video.thumb_data:
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
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{length}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(len(chunk))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/watch/<int:video_id>/stream")
@login_required
def video_stream(video_id):
    if not (current_user.is_authenticated and current_user.is_creator()):
        abort(404)
    video = db.session.get(Video, video_id)
    if video is None or not video.published:
        abort(404)
    if video.disk_name:
        path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"], video.disk_name)
        if not os.path.exists(path):
            abort(404)
        # send_file(conditional=True) handles Range requests (206 + Content-Range)
        resp = send_file(path, mimetype=video.mime, conditional=True,
                         download_name=video.filename or "video")
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    if video.data:  # legacy rows still stored in the database
        return _range_response(bytes(video.data), video.mime, video.filename or "video")
    abort(404)


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
