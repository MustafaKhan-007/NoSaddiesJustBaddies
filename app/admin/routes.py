"""Admin panel. Every route requires is_admin + recent admin activity.

Freshness is a *sliding* idle timeout: each admin action pushes the clock
forward, so day-to-day use never nags. Re-authentication is only required after
``ADMIN_IDLE_DAYS`` of no admin activity.
"""
import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Response, abort, current_app, flash, redirect,
                   render_template, request, session, stream_with_context,
                   url_for)
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (Announcement, CoachingRequest, FaqItem, ForumComment,
                      ForumPost, MEMBERSHIPS, MarketplaceListing, MembershipPlan,
                      Order, Page, Product, PRODUCT_SUBJECTS, ProductAsset, Quote,
                      QuoteFavorite, QuotePin, ReelReview, ReelReviewApplication,
                      Subscriber, Testimonial, User, Video, QUOTE_CATEGORIES)
from ..services import badges as badges_service
from ..services import quotes as quotes_service
from ..services import reel_reviews as reel_svc
from ..services import stats
from ..services.assets import AssetError, process_asset
from ..services.lemonsqueezy import sync_recent_orders
from ..services.settings import DEFAULTS as SETTING_DEFAULTS
from ..services.settings import all_settings, set_setting
from ..services.social import (fetch_instagram_preview, instagram_handle,
                               instagram_profile_url, platform_for)
from ..services.videos import (VideoError, delete_stored, process_thumb,
                               process_video)
from . import bp

log = logging.getLogger(__name__)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 404 (not 403) so the panel's existence isn't revealed
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(404)
        now = datetime.utcnow()
        idle_max = timedelta(days=current_app.config["ADMIN_IDLE_DAYS"])
        # last admin activity, falling back to the original sign-in time
        seen_at = session.get("admin_seen_at") or session.get("logged_in_at")
        try:
            active = seen_at and (now - datetime.fromisoformat(seen_at)) < idle_max
        except ValueError:
            active = False
        if not active:
            flash("It's been a while \u2014 please sign in again to open the studio.", "info")
            return redirect(url_for("auth.login", next=request.path))
        # slide the window forward on every admin action
        session.permanent = True
        session["admin_seen_at"] = now.isoformat()
        # keep the owner's stored tier at Creator so member-gated pages match
        if current_user.membership != "creator":
            current_user.membership = "creator"
            db.session.commit()
        return f(*args, **kwargs)
    return wrapper


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:150] or "item"


def _spotlight_candidates():
    """Creator members (and the owner) with an Instagram link — pick-list for
    Creator of the Month / Reel of the Week."""
    creators = (User.query.filter(
                    User.deleted_at.is_(None),
                    db.or_(User.membership == "creator", User.is_admin.is_(True)))
                .order_by(User.display_name).all())
    out = []
    for u in creators:
        handle = None
        for link in u.links():
            if platform_for(link["url"]) == "Instagram":
                handle = instagram_handle(link["url"]) or link["url"]
                break
        out.append({"name": u.public_name(), "email": u.email,
                    "instagram": f"@{handle}" if handle and not str(handle).startswith("http") else handle,
                    "profile_url": instagram_profile_url(handle) if handle else None})
    return out


# =============================== DASHBOARD ===================================

@bp.route("/")
@admin_required
def dashboard():
    product_id = None
    raw = request.args.get("product", "")
    if raw.isdigit():
        product_id = int(raw)
    selected_product = db.session.get(Product, product_id) if product_id else None
    if product_id and selected_product is None:
        product_id = None

    today = date.today()
    return render_template(
        "admin/dashboard.html",
        today_quote=quotes_service.quote_for(today),
        tomorrow_quote=quotes_service.quote_for(today + timedelta(days=1)),
        products=Product.query.order_by(Product.title).all(),
        selected_product=selected_product,
        cards=stats.dashboard_cards(product_id),
        lifetime=stats.lifetime_totals(product_id),
        chart_revenue=stats.revenue_by_day(90, product_id),
        chart_products=stats.orders_by_product(90),
        chart_signups=stats.signups_by_week(12),
        recent_orders=stats.recent_orders(10, product_id),
        top_products=stats.top_products(5),
        most_visited=stats.most_visited(7),
        memberships=stats.membership_breakdown(),
        video_count=stats.video_count(),
        marketplace=stats.marketplace_counts(),
    )


@bp.route("/sync", methods=["POST"])
@admin_required
def sync():
    result = sync_recent_orders()
    if result.get("ok"):
        flash(f"Synced {result['synced']} orders from Lemon Squeezy.", "success")
    else:
        flash(result.get("error", "Sync failed."), "error")
    return redirect(url_for("admin.dashboard"))


# =============================== PRODUCTS ====================================

@bp.route("/products")
@admin_required
def products():
    items = Product.query.order_by(Product.sort_order, Product.created_at.desc()).all()
    return render_template("admin/products.html", products=items)


@bp.route("/products/reorder", methods=["POST"])
@admin_required
def products_reorder():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    for position, pid in enumerate(ids):
        product = db.session.get(Product, int(pid))
        if product:
            product.sort_order = position
    db.session.commit()
    return {"ok": True}


def _product_from_form(product: Product, form) -> list[str]:
    """Apply form values to a product; returns a list of validation errors."""
    errors = []

    product.title = (form.get("title") or "").strip()[:160]
    if not product.title:
        errors.append("Title is required.")

    slug = slugify(form.get("slug") or product.title)
    clash = Product.query.filter(Product.slug == slug, Product.id != (product.id or 0)).first()
    if clash:
        errors.append(f'Slug "{slug}" is already taken.')
    product.slug = slug

    if form.get("type") in ("course", "guide"):
        product.type = form.get("type")
    subject = (form.get("subject") or "").strip()
    product.subject = subject if subject in PRODUCT_SUBJECTS else None
    product.featured = bool(form.get("featured"))
    product.badge = (form.get("badge") or "").strip()[:30] or None
    try:
        product.sort_order = int(form.get("sort_order") or 0)
    except ValueError:
        product.sort_order = 0

    promise = (form.get("promise") or "").strip()
    if len(promise) > 120:
        errors.append("The one-line promise must be 120 characters or fewer.")
    product.promise = promise[:120] or None
    product.description_md = form.get("description_md") or None
    product.audience = form.get("audience") or None
    product.contents_text = form.get("contents_text") or None

    titles = form.getlist("curriculum_title")
    descs = form.getlist("curriculum_desc")
    modules = [{"title": t.strip(), "description": d.strip()}
               for t, d in zip(titles, descs) if t.strip()]
    product.curriculum_json = json.dumps(modules) if modules else None

    cover = (form.get("cover_url") or "").strip()
    if cover and not cover.startswith("https://"):
        errors.append("Cover image URL must start with https://")
    product.cover_url = cover or None
    gallery = [u.strip() for u in (form.get("gallery_urls") or "").splitlines() if u.strip()]
    bad = [u for u in gallery if not u.startswith("https://")]
    if bad:
        errors.append("All gallery URLs must start with https://")
    product.gallery_json = json.dumps(gallery) if gallery else None

    price_raw = (form.get("price_cents") or "").strip()
    if price_raw:
        try:
            product.price_cents = int(price_raw)
            if product.price_cents < 0:
                raise ValueError
        except ValueError:
            errors.append("Price must be a whole number of cents (e.g. 2900 for $29).")
    else:
        product.price_cents = None
    compare_raw = (form.get("compare_at_cents") or "").strip()
    if compare_raw:
        try:
            product.compare_at_cents = int(compare_raw)
        except ValueError:
            errors.append("Compare-at price must be a whole number of cents.")
    else:
        product.compare_at_cents = None
    product.currency = (form.get("currency") or "USD").upper()[:3]

    ls_url = (form.get("ls_checkout_url") or "").strip()
    if ls_url and not ls_url.startswith("https://"):
        errors.append("The Lemon Squeezy buy link must start with https://")
    product.ls_checkout_url = ls_url or None
    product.ls_variant_id = (form.get("ls_variant_id") or "").strip() or None

    product.meta_title = (form.get("meta_title") or "").strip()[:160] or None
    product.meta_description = (form.get("meta_description") or "").strip()[:200] or None

    raw_tags = re.split(r"[,\n]", form.get("tags") or "")
    product.set_tags(raw_tags)

    status = form.get("status")
    if status in ("draft", "published", "archived"):
        if status == "published":
            blockers = product.publish_blockers()
            if blockers:
                errors.append("Can't publish yet \u2014 still missing: " + ", ".join(blockers) + ".")
                status = "draft"
        product.status = status
    return errors


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def product_form(product_id=None):
    product = db.session.get(Product, product_id) if product_id else Product()
    if product_id and product is None:
        abort(404)

    if request.method == "POST":
        errors = _product_from_form(product, request.form)
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "error")
        else:
            if product.id is None:
                db.session.add(product)
            db.session.flush()   # assign an id so assets can attach

            for aid in request.form.getlist("remove_asset"):
                if aid.isdigit():
                    asset = db.session.get(ProductAsset, int(aid))
                    if asset and asset.product_id == product.id:
                        db.session.delete(asset)

            asset_errors = []
            position = len(product.assets)
            for fs in request.files.getlist("asset_files"):
                if not fs or not fs.filename:
                    continue
                try:
                    data, mime, kind, fname = process_asset(fs)
                except AssetError as exc:
                    asset_errors.append(f"{fs.filename}: {exc}")
                    continue
                db.session.add(ProductAsset(
                    product_id=product.id, filename=fname, mime=mime, kind=kind,
                    size=len(data), data=data, sort_order=position))
                position += 1

            if asset_errors:
                db.session.rollback()
                for e in asset_errors:
                    flash(e, "error")
            else:
                db.session.commit()
                flash("Product saved.", "success")
                return redirect(url_for("admin.products"))

    curriculum = []
    if product.curriculum_json:
        try:
            curriculum = json.loads(product.curriculum_json)
        except ValueError:
            pass
    gallery = ""
    if product.gallery_json:
        try:
            gallery = "\n".join(json.loads(product.gallery_json))
        except ValueError:
            pass
    return render_template("admin/product_form.html", product=product,
                           curriculum=curriculum, gallery=gallery,
                           subjects=PRODUCT_SUBJECTS)


@bp.route("/products/<int:product_id>/delete", methods=["POST"])
@admin_required
def product_delete(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    if product.orders.count() > 0:
        flash("This product has orders \u2014 archive it instead of deleting.", "error")
        return redirect(url_for("admin.products"))
    db.session.delete(product)
    db.session.commit()
    flash("Product deleted.", "success")
    return redirect(url_for("admin.products"))


# ================================ QUOTES =====================================

@bp.route("/quotes")
@admin_required
def quotes():
    items = Quote.query.order_by(Quote.id.desc()).all()
    fav_counts = dict(
        db.session.query(QuoteFavorite.quote_id, func.count(QuoteFavorite.id))
        .group_by(QuoteFavorite.quote_id).all()
    )
    pins = QuotePin.query.filter(QuotePin.date >= date.today()).order_by(QuotePin.date).all()
    tomorrow = date.today() + timedelta(days=1)
    return render_template("admin/quotes.html", quotes=items, fav_counts=fav_counts,
                           pins=pins, tomorrow=tomorrow,
                           tomorrow_quote=quotes_service.quote_for(tomorrow),
                           categories=QUOTE_CATEGORIES)


@bp.route("/quotes/save", methods=["POST"])
@bp.route("/quotes/<int:quote_id>/save", methods=["POST"])
@admin_required
def quote_save(quote_id=None):
    quote = db.session.get(Quote, quote_id) if quote_id else Quote()
    if quote_id and quote is None:
        abort(404)
    text = (request.form.get("text") or "").strip()
    category = request.form.get("category")
    if not text or len(text) > 240:
        flash("Quote text is required (240 characters max).", "error")
        return redirect(url_for("admin.quotes"))
    if category not in QUOTE_CATEGORIES:
        flash("Pick a category.", "error")
        return redirect(url_for("admin.quotes"))
    quote.text = text
    quote.author = (request.form.get("author") or "").strip() or None
    quote.category = category
    quote.active = bool(request.form.get("active", quote_id is None))
    if quote.id is None:
        db.session.add(quote)
    db.session.commit()
    flash("Quote saved.", "success")
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/<int:quote_id>/toggle", methods=["POST"])
@admin_required
def quote_toggle(quote_id):
    quote = db.session.get(Quote, quote_id) or abort(404)
    quote.active = not quote.active
    db.session.commit()
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/<int:quote_id>/delete", methods=["POST"])
@admin_required
def quote_delete(quote_id):
    quote = db.session.get(Quote, quote_id) or abort(404)
    QuotePin.query.filter_by(quote_id=quote.id).delete()
    db.session.delete(quote)
    db.session.commit()
    flash("Quote deleted.", "success")
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/pin", methods=["POST"])
@admin_required
def quote_pin():
    try:
        pin_date = date.fromisoformat(request.form.get("date", ""))
        quote_id = int(request.form.get("quote_id", ""))
    except (ValueError, TypeError):
        flash("Pick a date and a quote to pin.", "error")
        return redirect(url_for("admin.quotes"))
    if db.session.get(Quote, quote_id) is None:
        abort(404)
    pin = QuotePin.query.filter_by(date=pin_date).first()
    if pin:
        pin.quote_id = quote_id
    else:
        db.session.add(QuotePin(date=pin_date, quote_id=quote_id))
    db.session.commit()
    flash(f"Pinned for {pin_date.isoformat()}.", "success")
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/pin/<int:pin_id>/delete", methods=["POST"])
@admin_required
def quote_unpin(pin_id):
    pin = db.session.get(QuotePin, pin_id) or abort(404)
    db.session.delete(pin)
    db.session.commit()
    flash("Pin removed \u2014 that day goes back to rotation.", "success")
    return redirect(url_for("admin.quotes"))


def _parse_import(raw: str):
    """`text | author | category` per line -> (rows, problems)."""
    rows, problems = [], []
    existing = {q.text.strip().lower() for q in Quote.query.all()}
    seen_in_batch = set()
    for i, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        text = parts[0] if parts else ""
        author = parts[1] if len(parts) > 1 and parts[1] else None
        category = (parts[2].lower() if len(parts) > 2 else "comfort")
        if not text or len(text) > 240:
            problems.append(f"Line {i}: text missing or over 240 chars \u2014 skipped.")
            continue
        if category not in QUOTE_CATEGORIES:
            problems.append(f'Line {i}: unknown category "{category}" \u2014 using comfort.')
            category = "comfort"
        key = text.lower()
        if key in existing or key in seen_in_batch:
            problems.append(f"Line {i}: duplicate \u2014 skipped.")
            continue
        seen_in_batch.add(key)
        rows.append({"text": text, "author": author, "category": category})
    return rows, problems


@bp.route("/quotes/import", methods=["POST"])
@admin_required
def quote_import():
    raw = request.form.get("bulk") or ""
    rows, problems = _parse_import(raw)
    if request.form.get("confirm") == "yes":
        for row in rows:
            db.session.add(Quote(**row))
        db.session.commit()
        flash(f"Imported {len(rows)} quotes." + (f" ({len(problems)} lines skipped.)" if problems else ""), "success")
        return redirect(url_for("admin.quotes"))
    return render_template("admin/quote_import_preview.html", rows=rows,
                           problems=problems, raw=raw)


# ============================ TESTIMONIALS ===================================

@bp.route("/testimonials")
@admin_required
def testimonials():
    items = Testimonial.query.order_by(Testimonial.sort_order).all()
    products = Product.query.order_by(Product.title).all()
    return render_template("admin/testimonials.html", items=items, products=products)


@bp.route("/testimonials/save", methods=["POST"])
@bp.route("/testimonials/<int:item_id>/save", methods=["POST"])
@admin_required
def testimonial_save(item_id=None):
    item = db.session.get(Testimonial, item_id) if item_id else Testimonial()
    if item_id and item is None:
        abort(404)
    quote = (request.form.get("quote") or "").strip()
    first_name = (request.form.get("first_name") or "").strip()[:60]
    if not quote or not first_name:
        flash("A testimonial needs both a quote and a first name.", "error")
        return redirect(url_for("admin.testimonials"))
    item.quote = quote
    item.first_name = first_name
    item.product_id = int(request.form["product_id"]) if request.form.get("product_id") else None
    item.show_on_home = bool(request.form.get("show_on_home"))
    try:
        item.sort_order = int(request.form.get("sort_order") or 0)
    except ValueError:
        item.sort_order = 0
    if item.id is None:
        db.session.add(item)
    db.session.commit()
    flash("Testimonial saved.", "success")
    return redirect(url_for("admin.testimonials"))


@bp.route("/testimonials/<int:item_id>/delete", methods=["POST"])
@admin_required
def testimonial_delete(item_id):
    item = db.session.get(Testimonial, item_id) or abort(404)
    db.session.delete(item)
    db.session.commit()
    flash("Testimonial deleted.", "success")
    return redirect(url_for("admin.testimonials"))


# ================================= FAQ =======================================

@bp.route("/faq")
@admin_required
def faq():
    items = FaqItem.query.order_by(FaqItem.sort_order).all()
    return render_template("admin/faq.html", items=items)


@bp.route("/faq/save", methods=["POST"])
@bp.route("/faq/<int:item_id>/save", methods=["POST"])
@admin_required
def faq_save(item_id=None):
    item = db.session.get(FaqItem, item_id) if item_id else FaqItem()
    if item_id and item is None:
        abort(404)
    question = (request.form.get("question") or "").strip()[:240]
    answer = (request.form.get("answer_md") or "").strip()
    if not question or not answer:
        flash("A FAQ item needs both a question and an answer.", "error")
        return redirect(url_for("admin.faq"))
    item.question = question
    item.answer_md = answer
    try:
        item.sort_order = int(request.form.get("sort_order") or 0)
    except ValueError:
        item.sort_order = 0
    if item.id is None:
        db.session.add(item)
    db.session.commit()
    flash("FAQ saved.", "success")
    return redirect(url_for("admin.faq"))


@bp.route("/faq/<int:item_id>/delete", methods=["POST"])
@admin_required
def faq_delete(item_id):
    item = db.session.get(FaqItem, item_id) or abort(404)
    db.session.delete(item)
    db.session.commit()
    flash("FAQ item deleted.", "success")
    return redirect(url_for("admin.faq"))


# ================================ PAGES ======================================

EDITABLE_PAGES = (
    ("about", "Her Story (About page)"),
    ("privacy", "Privacy Policy"),
    ("terms", "Terms of Service"),
    ("refunds", "Refund Policy"),
)


@bp.route("/pages")
@admin_required
def pages():
    existing = {p.slug: p for p in Page.query.all()}
    return render_template("admin/pages.html", editable=EDITABLE_PAGES, existing=existing)


@bp.route("/pages/<slug>", methods=["GET", "POST"])
@admin_required
def page_edit(slug):
    labels = dict(EDITABLE_PAGES)
    if slug not in labels:
        abort(404)
    page = Page.query.filter_by(slug=slug).first()
    if request.method == "POST":
        title = (request.form.get("title") or labels[slug]).strip()[:160]
        body = request.form.get("body_md") or ""
        if page is None:
            page = Page(slug=slug, title=title, body_md=body)
            db.session.add(page)
        else:
            page.title = title
            page.body_md = body
        db.session.commit()
        flash("Page saved.", "success")
        return redirect(url_for("admin.pages"))
    return render_template("admin/page_form.html", page=page, slug=slug, label=labels[slug])


# ============================= SUBSCRIBERS ===================================

@bp.route("/subscribers")
@admin_required
def subscribers():
    items = Subscriber.query.order_by(Subscriber.created_at.desc()).all()
    return render_template("admin/subscribers.html", items=items)


@bp.route("/subscribers/export.csv")
@admin_required
def subscribers_export():
    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["email", "subscribed_at"])
        yield buffer.getvalue()
        for sub in Subscriber.query.order_by(Subscriber.created_at).yield_per(200):
            buffer.seek(0)
            buffer.truncate()
            writer.writerow([sub.email, sub.created_at.isoformat()])
            yield buffer.getvalue()
    return Response(stream_with_context(generate()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=subscribers.csv"})


@bp.route("/subscribers/<int:sub_id>/delete", methods=["POST"])
@admin_required
def subscriber_delete(sub_id):
    sub = db.session.get(Subscriber, sub_id) or abort(404)
    db.session.delete(sub)
    db.session.commit()
    flash("Subscriber removed.", "success")
    return redirect(url_for("admin.subscribers"))


# ================================ ORDERS =====================================

def _orders_query():
    query = Order.query
    product_id = request.args.get("product")
    if product_id and product_id.isdigit():
        query = query.filter(Order.product_id == int(product_id))
    for arg, op in (("from", ">="), ("to", "<=")):
        raw = request.args.get(arg)
        if raw:
            try:
                day = date.fromisoformat(raw)
                if op == ">=":
                    query = query.filter(Order.created_at >= datetime.combine(day, datetime.min.time()))
                else:
                    query = query.filter(Order.created_at <= datetime.combine(day, datetime.max.time()))
            except ValueError:
                pass
    return query


@bp.route("/orders")
@admin_required
def orders():
    items = (_orders_query().options(joinedload(Order.product))
             .order_by(Order.created_at.desc()).limit(500).all())
    products = Product.query.order_by(Product.title).all()
    return render_template("admin/orders.html", items=items, products=products)


@bp.route("/orders/export.csv")
@admin_required
def orders_export():
    query = _orders_query().order_by(Order.created_at)

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["date", "ls_order_id", "product", "buyer_email", "total", "currency", "status"])
        yield buffer.getvalue()
        for order in query.yield_per(200):
            buffer.seek(0)
            buffer.truncate()
            writer.writerow([
                order.created_at.isoformat(), order.ls_order_id,
                order.product.title if order.product else "",
                order.buyer_email, f"{order.total_cents / 100:.2f}",
                order.currency, order.status,
            ])
            yield buffer.getvalue()
    return Response(stream_with_context(generate()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=orders.csv"})


# =============================== SETTINGS ====================================

@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        if request.form.get("clear_announcement"):
            set_setting("announcement_text", "")
            set_setting("announcement_expires", "")
            flash("Announcement removed.", "success")
            return redirect(url_for("admin.settings"))
        if request.form.get("add_announcement"):
            body = (request.form.get("ann_body") or "").strip()[:300]
            if body:
                expires = date.today() + timedelta(days=1)  # default: 1 day
                raw = (request.form.get("ann_expires") or "").strip()
                if raw:
                    try:
                        expires = date.fromisoformat(raw)
                    except ValueError:
                        pass
                db.session.add(Announcement(body=body, expires=expires))
                db.session.commit()
                flash("Announcement added.", "success")
            else:
                flash("Write something first.", "error")
            return redirect(url_for("admin.settings"))
        remove_id = request.form.get("remove_announcement")
        if remove_id and remove_id.isdigit():
            ann = db.session.get(Announcement, int(remove_id))
            if ann:
                db.session.delete(ann)
                db.session.commit()
            flash("Announcement removed.", "success")
            return redirect(url_for("admin.settings"))
        values = {key: (request.form.get(key) or "").strip()
                  for key in SETTING_DEFAULTS}
        # store a clean Instagram handle (never a share-link with ?igsh=…)
        handle = instagram_handle(values.get("creator_instagram") or "")
        values["creator_instagram"] = handle
        # if photo/bio were left blank, try a public Instagram preview
        if handle and (not values.get("creator_image_url")
                       or not values.get("creator_blurb")):
            preview = fetch_instagram_preview(handle)
            if preview.get("image") and not values.get("creator_image_url"):
                values["creator_image_url"] = preview["image"]
            if preview.get("blurb") and not values.get("creator_blurb"):
                values["creator_blurb"] = preview["blurb"]
        # quick announcement: blank expiry defaults to tomorrow
        if values.get("announcement_text") and not values.get("announcement_expires"):
            values["announcement_expires"] = (date.today() + timedelta(days=1)).isoformat()
        for key, val in values.items():
            set_setting(key, val)
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    values = all_settings()
    # show a friendly @handle in the form even if an old full URL is stored
    if values.get("creator_instagram"):
        h = instagram_handle(values["creator_instagram"])
        values["creator_instagram"] = f"@{h}" if h else values["creator_instagram"]
    announcements = (Announcement.query
                     .order_by(Announcement.sort_order, Announcement.created_at.desc()).all())
    default_expires = (date.today() + timedelta(days=1)).isoformat()
    return render_template("admin/settings.html", values=values,
                           spotlight=_spotlight_candidates(),
                           announcements=announcements, today=date.today(),
                           default_expires=default_expires)


# ============================ MARKETPLACE ====================================

@bp.route("/marketplace")
@admin_required
def marketplace():
    listings = (MarketplaceListing.query
                .order_by(MarketplaceListing.active.desc(),
                          MarketplaceListing.created_at.desc()).all())
    return render_template("admin/marketplace.html", listings=listings)


@bp.route("/marketplace/<int:listing_id>/toggle", methods=["POST"])
@admin_required
def marketplace_toggle(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id) or abort(404)
    ln.active = not ln.active
    db.session.commit()
    flash("Listing hidden." if not ln.active else "Listing restored.", "success")
    return redirect(url_for("admin.marketplace"))


@bp.route("/marketplace/<int:listing_id>/delete", methods=["POST"])
@admin_required
def marketplace_delete(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id) or abort(404)
    db.session.delete(ln)
    db.session.commit()
    flash("Listing deleted.", "success")
    return redirect(url_for("admin.marketplace"))


# =============================== VIDEOS ======================================

@bp.route("/videos")
@admin_required
def videos():
    items = Video.query.order_by(Video.sort_order, Video.created_at.desc()).all()
    return render_template("admin/videos.html", videos=items)


@bp.route("/videos/new", methods=["GET", "POST"])
@bp.route("/videos/<int:video_id>/edit", methods=["GET", "POST"])
@admin_required
def video_form(video_id=None):
    video = db.session.get(Video, video_id) if video_id else None
    if video_id and video is None:
        abort(404)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:160]
        description = (request.form.get("description") or "").strip() or None
        published = bool(request.form.get("published"))
        try:
            sort_order = int(request.form.get("sort_order") or 0)
        except ValueError:
            sort_order = 0

        errors = []
        if not title:
            errors.append("A title is required.")

        new_video = None
        upload = request.files.get("video_file")
        if upload and upload.filename:
            try:
                new_video = process_video(
                    upload, current_app.config["VIDEO_STORAGE_DIR"],
                    current_app.config["MAX_VIDEO_MB"] * 1024 * 1024)
            except VideoError as exc:
                errors.append(str(exc))
        elif video is None:
            errors.append("Please choose a video file to upload.")

        new_thumb = None
        thumb = request.files.get("thumb_file")
        if thumb and thumb.filename:
            try:
                new_thumb = process_thumb(thumb)
            except VideoError as exc:
                errors.append(str(exc))

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            old_disk = None
            try:
                if video is None:
                    video = Video(mime="video/mp4")
                    db.session.add(video)
                video.title = title
                video.description = description
                video.published = published
                video.sort_order = sort_order
                if new_video:
                    disk_name, mime, fname, size = new_video
                    old_disk = video.disk_name  # replaced file, delete after commit
                    video.disk_name, video.mime, video.filename = disk_name, mime, fname
                    video.size = size
                    video.data = None
                if new_thumb:
                    video.thumb_data, video.thumb_mime = new_thumb
                if request.form.get("remove_thumb"):
                    video.thumb_data = None
                    video.thumb_mime = None
                db.session.commit()
            except Exception:
                db.session.rollback()
                log.exception("video upload failed")
                # a brand-new file we just wrote is now orphaned; clean it up
                if new_video:
                    delete_stored(current_app.config["VIDEO_STORAGE_DIR"], new_video[0])
                flash("We couldn't save that video just now \u2014 please try again.",
                      "error")
            else:
                if old_disk:
                    delete_stored(current_app.config["VIDEO_STORAGE_DIR"], old_disk)
                flash("Video saved.", "success")
                return redirect(url_for("admin.videos"))

    return render_template("admin/video_form.html", video=video,
                           max_mb=current_app.config["MAX_VIDEO_MB"])


@bp.route("/videos/<int:video_id>/delete", methods=["POST"])
@admin_required
def video_delete(video_id):
    video = db.session.get(Video, video_id) or abort(404)
    disk_name = video.disk_name
    db.session.delete(video)
    db.session.commit()
    delete_stored(current_app.config["VIDEO_STORAGE_DIR"], disk_name)
    flash("Video deleted.", "success")
    return redirect(url_for("admin.videos"))


# =============================== MEMBERS =====================================

@bp.route("/members")
@admin_required
def members():
    q = (request.args.get("q") or "").strip()
    query = User.query.filter(User.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.email.ilike(like),
                                    User.display_name.ilike(like)))
    people = query.order_by(User.created_at.desc()).limit(200).all()
    counts = dict(db.session.query(User.membership, func.count(User.id))
                  .filter(User.deleted_at.is_(None)).group_by(User.membership).all())
    return render_template("admin/members.html", people=people, counts=counts,
                           memberships=MEMBERSHIPS, q=q,
                           spotlight=_spotlight_candidates())


@bp.route("/members/<int:user_id>/membership", methods=["POST"])
@admin_required
def set_membership(user_id):
    member = db.session.get(User, user_id) or abort(404)
    if member.is_admin:
        flash("The owner account always keeps Creator access.", "info")
        return redirect(request.form.get("next") or url_for("admin.members"))
    tier = request.form.get("membership")
    if tier in MEMBERSHIPS:
        member.membership = tier
        from ..services.listings import enforce_listing_limits
        enforce_listing_limits(member)
        db.session.commit()
        flash(f"{member.public_name()} \u2192 {member.membership_label()}.", "success")
    return redirect(request.form.get("next") or url_for("admin.members"))


# ============================ MEMBERSHIP PLANS ===============================

_PLAN_DEFAULTS = {
    "healing": {"name": "Healing membership",
                "tagline": "Belong to the whole community.", "sort_order": 1},
    "creator": {"name": "Creator membership",
                "tagline": "Everything, plus the tools to be seen.", "sort_order": 2},
}


def _get_plans():
    """Return the two membership plans, creating any that are missing."""
    plans = {p.tier: p for p in MembershipPlan.query.all()}
    changed = False
    for tier, d in _PLAN_DEFAULTS.items():
        if tier not in plans:
            plan = MembershipPlan(tier=tier, name=d["name"], tagline=d["tagline"],
                                  sort_order=d["sort_order"])
            db.session.add(plan)
            plans[tier] = plan
            changed = True
    if changed:
        db.session.commit()
    return [plans["healing"], plans["creator"]]


@bp.route("/memberships", methods=["GET", "POST"])
@admin_required
def membership_plans():
    plans = _get_plans()
    if request.method == "POST":
        for plan in plans:
            p = plan.tier
            plan.name = (request.form.get(f"{p}_name") or plan.name).strip()
            plan.tagline = (request.form.get(f"{p}_tagline") or "").strip() or None
            plan.currency = (request.form.get(f"{p}_currency") or "USD").strip().upper()[:3]
            plan.period = request.form.get(f"{p}_period") or "month"
            plan.ls_variant_id = (request.form.get(f"{p}_variant") or "").strip() or None
            plan.ls_checkout_url = (request.form.get(f"{p}_checkout") or "").strip() or None
            plan.active = bool(request.form.get(f"{p}_active"))
            raw = (request.form.get(f"{p}_price") or "").strip().replace(",", "")
            try:
                plan.price_cents = round(float(raw) * 100) if raw else None
            except ValueError:
                plan.price_cents = plan.price_cents
        db.session.commit()
        flash("Membership plans saved.", "success")
        return redirect(url_for("admin.membership_plans"))
    return render_template("admin/membership_plans.html", plans=plans)


# ================================ BADGES =====================================

@bp.route("/badges", methods=["GET", "POST"])
@admin_required
def badges():
    if request.method == "POST":
        if request.form.get("reset"):
            badges_service.reset_thresholds()
            flash("Milestones reset to their defaults.", "success")
            return redirect(url_for("admin.badges"))

        mapping, errors = {}, []
        for cat_key, cat in badges_service.CATEGORIES.items():
            values = []
            for level in range(1, len(cat["tiers"]) + 1):
                raw = (request.form.get(f"t_{cat_key}_{level}") or "").strip()
                try:
                    n = int(raw)
                except ValueError:
                    errors.append(f"{cat['name']}: milestone {level} must be a whole number.")
                    break
                if n < 1:
                    errors.append(f"{cat['name']}: milestones must be at least 1.")
                    break
                if values and n <= values[-1]:
                    errors.append(f"{cat['name']}: each milestone must be higher than the one before.")
                    break
                values.append(n)
            if len(values) == len(cat["tiers"]):
                mapping[cat_key] = values

        if errors:
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("admin.badges"))

        badges_service.set_thresholds(mapping)
        flash("Milestones saved.", "success")
        return redirect(url_for("admin.badges"))

    return render_template("admin/badges.html",
                           overview=badges_service.all_badges_overview(),
                           owner_badge=badges_service.OWNER_BADGE)


# =============================== COMMUNITY ===================================

@bp.route("/community")
@admin_required
def community():
    posts = (ForumPost.query.options(joinedload(ForumPost.category),
                                     joinedload(ForumPost.author))
             .order_by(ForumPost.created_at.desc()).limit(100).all())
    flagged = (User.query.filter((User.forum_warnings > 0) | (User.forum_banned.is_(True)))
               .order_by(User.forum_banned.desc(), User.forum_warnings.desc()).all())
    return render_template("admin/community.html", posts=posts, flagged=flagged)


@bp.route("/community/post/<int:post_id>/delete", methods=["POST"])
@admin_required
def community_delete_post(post_id):
    post = db.session.get(ForumPost, post_id) or abort(404)
    db.session.delete(post)
    db.session.commit()
    flash("Post removed.", "success")
    return redirect(url_for("admin.community"))


@bp.route("/community/comment/<int:comment_id>/delete", methods=["POST"])
@admin_required
def community_delete_comment(comment_id):
    comment = db.session.get(ForumComment, comment_id) or abort(404)
    db.session.delete(comment)
    db.session.commit()
    flash("Comment removed.", "success")
    return redirect(url_for("admin.community"))


@bp.route("/community/member/<int:user_id>/reset", methods=["POST"])
@admin_required
def community_reset_member(user_id):
    member = db.session.get(User, user_id) or abort(404)
    member.forum_warnings = 0
    member.forum_banned = False
    db.session.commit()
    flash("Fresh start given \u2014 warnings cleared and posting restored.", "success")
    return redirect(url_for("admin.community"))


# ============================ REEL REVIEWS ===================================

@bp.route("/reel-reviews")
@admin_required
def reel_reviews():
    week = reel_svc.current_week_key()
    applicants = reel_svc.week_applicants(week)
    published = (ReelReview.query
                 .order_by(ReelReview.created_at.desc()).limit(40).all())
    return render_template("admin/reel_reviews.html", week_key=week,
                           applicants=applicants, reviews=published,
                           max_mb=current_app.config["MAX_VIDEO_MB"])


@bp.route("/reel-reviews/pick", methods=["POST"])
@admin_required
def reel_reviews_pick():
    chosen = reel_svc.pick_random_applicant()
    if chosen is None:
        flash("No applicants in this week's draw yet.", "error")
    else:
        flash(f"Selected {chosen.author.public_name()} for this week's review.",
              "success")
    return redirect(url_for("admin.reel_reviews"))


@bp.route("/reel-reviews/<int:app_id>/publish", methods=["POST"])
@admin_required
def reel_reviews_publish(app_id):
    application = db.session.get(ReelReviewApplication, app_id) or abort(404)
    title = (request.form.get("title") or "").strip()[:160]
    body = (request.form.get("body") or "").strip()
    if not title:
        flash("Give the review a title.", "error")
        return redirect(url_for("admin.reel_reviews"))
    review = application.review or ReelReview(application_id=application.id)
    if application.review is None:
        db.session.add(review)
    review.title = title
    review.body = body or ""
    review.published = True
    upload = request.files.get("review_video")
    if upload and upload.filename:
        try:
            disk_name, mime, fname, _size = process_video(
                upload, current_app.config["VIDEO_STORAGE_DIR"],
                current_app.config["MAX_VIDEO_MB"] * 1024 * 1024)
        except VideoError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.reel_reviews"))
        if review.review_disk_name:
            delete_stored(current_app.config["VIDEO_STORAGE_DIR"],
                          review.review_disk_name)
        review.review_disk_name = disk_name
        review.review_mime = mime
        review.review_filename = fname
    application.selected = True
    db.session.commit()
    flash("Reel review published to the Content Hub.", "success")
    return redirect(url_for("admin.reel_reviews"))


@bp.route("/reel-reviews/review/<int:review_id>/unpublish", methods=["POST"])
@admin_required
def reel_reviews_unpublish(review_id):
    review = db.session.get(ReelReview, review_id) or abort(404)
    review.published = False
    db.session.commit()
    flash("Review hidden from the Content Hub.", "success")
    return redirect(url_for("admin.reel_reviews"))


# =============================== COACHING ====================================

@bp.route("/coaching")
@admin_required
def coaching():
    rows = (CoachingRequest.query.options(joinedload(CoachingRequest.author))
            .order_by(CoachingRequest.created_at.desc()).limit(100).all())
    return render_template("admin/coaching.html", requests=rows)


@bp.route("/coaching/<int:req_id>/status", methods=["POST"])
@admin_required
def coaching_status(req_id):
    row = db.session.get(CoachingRequest, req_id) or abort(404)
    status = (request.form.get("status") or "").strip()
    if status not in ("pending", "booked", "done", "cancelled"):
        flash("Unknown status.", "error")
    else:
        row.status = status
        db.session.commit()
        flash("Coaching request updated.", "success")
    return redirect(url_for("admin.coaching"))
