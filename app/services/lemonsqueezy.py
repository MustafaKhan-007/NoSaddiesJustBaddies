"""Lemon Squeezy REST API reconciliation (dashboard "Sync" button).

Webhooks are the primary data source; this repairs drift by fetching recent
orders and upserting by `ls_order_id`.
"""
import logging

import requests
from flask import current_app

from ..extensions import db
from ..models import Order, Product

log = logging.getLogger(__name__)

API_BASE = "https://api.lemonsqueezy.com/v1"
MAX_PAGES = 5
PAGE_SIZE = 50
TIMEOUT = 15


def _product_for_variant(variant_id: str | None) -> Product | None:
    """Match a store product by Lemon variant ID (string-normalized)."""
    if not variant_id:
        return None
    key = str(variant_id).strip()
    if not key:
        return None
    return Product.query.filter_by(ls_variant_id=key).first()


def upsert_order(ls_order_id: str, ls_variant_id: str | None, buyer_email: str,
                 total_cents: int, currency: str, status: str, created_at=None,
                 gift_to: str | None = None) -> Order:
    """Insert or update an order row; idempotent on ls_order_id.

    When the variant matches a Studio product, ``product_id`` is set so the
    purchase appears in the buyer's My space library (read online — no download).
    """
    order = Order.query.filter_by(ls_order_id=str(ls_order_id)).first()
    if order is None:
        order = Order(ls_order_id=str(ls_order_id))
        db.session.add(order)
    if ls_variant_id is not None and str(ls_variant_id).strip():
        order.ls_variant_id = str(ls_variant_id).strip()
    order.buyer_email = (buyer_email or "").strip().lower()
    if gift_to:
        order.gift_to_email = gift_to.strip().lower()
    order.total_cents = total_cents
    order.currency = (currency or "USD").upper()
    order.status = status
    if created_at is not None:
        order.created_at = created_at
    if order.ls_variant_id and order.product_id is None:
        product = _product_for_variant(order.ls_variant_id)
        if product:
            order.product_id = product.id
        else:
            log.warning("webhook/sync: no product with ls_variant_id=%s "
                        "(order %s won't appear in My space until it's set)",
                        order.ls_variant_id, order.ls_order_id)
    # grant/revoke membership when the purchased variant is a membership plan
    from .memberships import apply_from_order
    apply_from_order(order)
    return order


def sync_recent_orders() -> dict:
    """Fetch recent orders from the LS API and upsert. Returns a summary."""
    api_key = current_app.config["LEMONSQUEEZY_API_KEY"]
    if not api_key:
        return {"ok": False, "error": "LEMONSQUEEZY_API_KEY is not configured."}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/vnd.api+json",
    }
    url = f"{API_BASE}/orders"
    params = {"page[size]": PAGE_SIZE, "sort": "-createdAt"}

    seen = 0
    try:
        for _ in range(MAX_PAGES):
            resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("data", []):
                attrs = item.get("attributes", {})
                first_item = attrs.get("first_order_item") or {}
                upsert_order(
                    ls_order_id=item.get("id"),
                    ls_variant_id=first_item.get("variant_id"),
                    buyer_email=attrs.get("user_email") or "",
                    total_cents=int(attrs.get("total") or 0),
                    currency=attrs.get("currency") or "USD",
                    status=attrs.get("status") or "paid",
                )
                seen += 1
            next_url = (payload.get("links") or {}).get("next")
            if not next_url:
                break
            url, params = next_url, {}
        db.session.commit()
        return {"ok": True, "synced": seen}
    except requests.RequestException as exc:
        db.session.rollback()
        log.exception("Lemon Squeezy sync failed")
        return {"ok": False, "error": f"Lemon Squeezy API error: {exc}"}
