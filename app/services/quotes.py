"""Daily quote rotation."""
import hashlib
from datetime import date, timedelta

from ..extensions import db
from ..models import Quote, QuotePin

#: weekly tone rhythm — Monday/Tuesday lean determination, weekend leans comfort
CATEGORY_OF_WEEKDAY = {
    0: "determination",  # Monday
    1: "determination",  # Tuesday
    5: "comfort",        # Saturday
    6: "comfort",        # Sunday
}


def _pick_from_pool(day: date, pool: list[Quote]) -> Quote:
    """Deterministically choose a quote for `day` from a non-empty active pool.

    Filtered by the day's category when the weekday has one (falling back to the
    whole pool if that category is empty), then indexed by a stable hash of the
    ISO date so everyone sees the same quote all day and restarts don't change it.
    """
    category = CATEGORY_OF_WEEKDAY.get(day.weekday())
    if category:
        filtered = [q for q in pool if q.category == category]
        if filtered:
            pool = filtered
    digest = hashlib.sha256(day.isoformat().encode()).hexdigest()
    return pool[int(digest, 16) % len(pool)]


def quote_for(day: date, count_view: bool = False) -> Quote | None:
    """Deterministic quote for a date. A `QuotePin` overrides rotation."""
    pin = QuotePin.query.filter_by(date=day).first()
    if pin and pin.quote and pin.quote.active:
        quote = pin.quote
    else:
        pool = Quote.query.filter_by(active=True).order_by(Quote.id).all()
        if not pool:
            return None
        quote = _pick_from_pool(day, pool)

    if count_view and quote.last_shown_date != day:
        quote.last_shown_date = day
        quote.times_shown = (quote.times_shown or 0) + 1
        db.session.commit()
    return quote


def recent_quotes(days: int = 30, today: date | None = None):
    """[(date, Quote)] for the last `days` days, newest first.

    The active pool and any pins in range are loaded once (two queries total)
    rather than re-querying per day.
    """
    today = today or date.today()
    pool = Quote.query.filter_by(active=True).order_by(Quote.id).all()
    if not pool:
        return []
    oldest = today - timedelta(days=days - 1)
    pins = {p.date: p.quote for p in
            QuotePin.query.filter(QuotePin.date >= oldest,
                                  QuotePin.date <= today).all()}
    out = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        pinned = pins.get(day)
        if pinned is not None and pinned.active:
            out.append((day, pinned))
        else:
            out.append((day, _pick_from_pool(day, pool)))
    return out
