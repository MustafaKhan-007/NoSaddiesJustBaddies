"""Dashboard statistics, computed from the local database only."""
from datetime import date, datetime, time, timedelta

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (ForumPost, Order, PageView, Product, Subscriber, User,
                      Video)

PAID_STATUSES = ("paid",)


def _dt(day: date) -> datetime:
    return datetime.combine(day, time.min)


def _product_filter(query, product_id):
    if product_id:
        return query.filter(Order.product_id == product_id)
    return query


def _window_sums(days: int = 30, product_id: int | None = None) -> dict:
    """Revenue/orders/subscribers for the last `days` days + previous window."""
    today = date.today()
    cur_start = _dt(today - timedelta(days=days - 1))
    prev_start = _dt(today - timedelta(days=2 * days - 1))

    def revenue(start, end):
        return _product_filter(
            db.session.query(func.coalesce(func.sum(Order.total_cents), 0)).filter(
                Order.status.in_(PAID_STATUSES), Order.created_at >= start, Order.created_at < end
            ), product_id).scalar()

    def orders(start, end):
        return _product_filter(
            db.session.query(func.count(Order.id)).filter(
                Order.status.in_(PAID_STATUSES), Order.created_at >= start, Order.created_at < end
            ), product_id).scalar()

    def subs(start, end):
        return db.session.query(func.count(Subscriber.id)).filter(
            Subscriber.created_at >= start, Subscriber.created_at < end
        ).scalar()

    now = _dt(today + timedelta(days=1))
    return {
        "revenue_cents": revenue(cur_start, now),
        "revenue_prev_cents": revenue(prev_start, cur_start),
        "orders": orders(cur_start, now),
        "orders_prev": orders(prev_start, cur_start),
        "subscribers": subs(cur_start, now),
        "subscribers_prev": subs(prev_start, cur_start),
    }


def dashboard_cards(product_id: int | None = None) -> dict:
    sums = _window_sums(30, product_id)
    posts_30d = db.session.query(func.count(ForumPost.id)).filter(
        ForumPost.created_at >= _dt(date.today() - timedelta(days=29))
    ).scalar()

    def delta(cur, prev):
        if prev == 0:
            return None
        return round((cur - prev) / prev * 100)

    return {
        "revenue": sums["revenue_cents"] / 100,
        "revenue_delta": delta(sums["revenue_cents"], sums["revenue_prev_cents"]),
        "orders": sums["orders"],
        "orders_delta": delta(sums["orders"], sums["orders_prev"]),
        "subscribers": sums["subscribers"],
        "subscribers_delta": delta(sums["subscribers"], sums["subscribers_prev"]),
        "forum_posts": posts_30d,
    }


def revenue_by_day(days: int = 90, product_id: int | None = None) -> dict:
    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = _product_filter(
        db.session.query(
            func.date(Order.created_at).label("day"),
            func.sum(Order.total_cents),
        ).filter(
            Order.status.in_(PAID_STATUSES), Order.created_at >= _dt(start)
        ), product_id).group_by("day").all()
    by_day = {str(day): (cents or 0) / 100 for day, cents in rows}
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    return {"labels": labels, "values": [by_day.get(d, 0) for d in labels]}


def orders_by_product(days: int = 90) -> dict:
    start = _dt(date.today() - timedelta(days=days - 1))
    rows = db.session.query(
        Product.title, func.count(Order.id)
    ).join(Order, Order.product_id == Product.id).filter(
        Order.status.in_(PAID_STATUSES), Order.created_at >= start
    ).group_by(Product.id).order_by(func.count(Order.id).desc()).limit(10).all()
    unmatched = db.session.query(func.count(Order.id)).filter(
        Order.status.in_(PAID_STATUSES), Order.created_at >= start, Order.product_id.is_(None)
    ).scalar()
    labels = [title for title, _ in rows]
    values = [count for _, count in rows]
    if unmatched:
        labels.append("(unmatched)")
        values.append(unmatched)
    return {"labels": labels, "values": values}


def signups_by_week(weeks: int = 12) -> dict:
    today = date.today()
    start = today - timedelta(weeks=weeks)
    labels, users, subs = [], [], []
    for i in range(weeks):
        week_start = start + timedelta(weeks=i)
        week_end = week_start + timedelta(weeks=1)
        labels.append(week_start.isoformat())
        users.append(db.session.query(func.count(User.id)).filter(
            User.created_at >= _dt(week_start), User.created_at < _dt(week_end)
        ).scalar())
        subs.append(db.session.query(func.count(Subscriber.id)).filter(
            Subscriber.created_at >= _dt(week_start), Subscriber.created_at < _dt(week_end)
        ).scalar())
    return {"labels": labels, "users": users, "subscribers": subs}


def recent_orders(limit: int = 10, product_id: int | None = None):
    return _product_filter(Order.query.options(joinedload(Order.product)), product_id)\
        .order_by(Order.created_at.desc()).limit(limit).all()


def lifetime_totals(product_id: int | None = None) -> dict:
    """All-time revenue and order count (optionally for one product)."""
    revenue = _product_filter(
        db.session.query(func.coalesce(func.sum(Order.total_cents), 0)).filter(
            Order.status.in_(PAID_STATUSES)), product_id).scalar()
    orders = _product_filter(
        db.session.query(func.count(Order.id)).filter(
            Order.status.in_(PAID_STATUSES)), product_id).scalar()
    return {"revenue": revenue / 100, "orders": orders}


def top_products(limit: int = 5):
    return db.session.query(
        Product, func.count(Order.id).label("n"), func.sum(Order.total_cents).label("cents")
    ).join(Order, Order.product_id == Product.id).filter(
        Order.status.in_(PAID_STATUSES)
    ).group_by(Product.id).order_by(func.count(Order.id).desc()).limit(limit).all()


def membership_breakdown() -> dict:
    rows = dict(db.session.query(User.membership, func.count(User.id))
                .filter(User.deleted_at.is_(None)).group_by(User.membership).all())
    return {
        "none": rows.get("none", 0),
        "healing": rows.get("healing", 0),
        "creator": rows.get("creator", 0),
        "total": sum(rows.values()),
    }


def video_count() -> int:
    return db.session.query(func.count(Video.id)).scalar() or 0


def most_visited(days: int = 7, limit: int = 10):
    start = date.today() - timedelta(days=days - 1)
    return db.session.query(
        PageView.path, func.sum(PageView.count).label("views")
    ).filter(PageView.date >= start).group_by(PageView.path).order_by(
        func.sum(PageView.count).desc()
    ).limit(limit).all()
