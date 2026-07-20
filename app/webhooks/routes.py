"""Lemon Squeezy webhook receiver.

Verifies X-Signature (HMAC-SHA256 of the raw body with the webhook secret),
handles order_created / order_refunded, and is idempotent on ls_order_id.
"""
import hashlib
import hmac
import logging

from flask import current_app, request

from ..extensions import db
from ..services.lemonsqueezy import upsert_order
from . import bp

log = logging.getLogger(__name__)

HANDLED_EVENTS = {"order_created", "order_refunded"}


def _signature_valid(raw_body: bytes, signature: str) -> bool:
    secret = current_app.config["LEMONSQUEEZY_WEBHOOK_SECRET"]
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


@bp.route("/lemonsqueezy", methods=["POST"])
def lemonsqueezy():
    raw = request.get_data()
    if not _signature_valid(raw, request.headers.get("X-Signature", "")):
        log.warning("webhook: invalid signature (ip=%s)", request.remote_addr)
        return {"error": "invalid signature"}, 401

    payload = request.get_json(silent=True) or {}
    event = (
        request.headers.get("X-Event-Name")
        or (payload.get("meta") or {}).get("event_name")
        or ""
    )
    if event not in HANDLED_EVENTS:
        return {"status": "ignored", "event": event}, 200

    try:
        data = payload.get("data") or {}
        attrs = data.get("attributes") or {}
        first_item = attrs.get("first_order_item") or {}
        status = attrs.get("status") or ("refunded" if event == "order_refunded" else "paid")
        # Lemon may put custom fields on meta and/or attributes
        custom = {}
        custom.update((payload.get("meta") or {}).get("custom_data") or {})
        custom.update(attrs.get("custom_data") or {})
        gift_to = custom.get("gift_to") or custom.get("giftTo") or None

        upsert_order(
            ls_order_id=data.get("id"),
            ls_variant_id=first_item.get("variant_id"),
            buyer_email=attrs.get("user_email") or "",
            total_cents=int(attrs.get("total") or 0),
            currency=attrs.get("currency") or "USD",
            status=status,
            gift_to=gift_to,
        )
        db.session.commit()
        log.info("webhook: %s processed (order %s)", event, data.get("id"))
        return {"status": "ok"}, 200
    except Exception:
        db.session.rollback()
        log.exception("webhook: failed to process %s", event)
        return {"error": "processing failed"}, 500
