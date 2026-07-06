"""Daily quote rotation, check-ins and streaks."""
import hashlib
from datetime import date, timedelta

from ..extensions import db
from ..models import CheckIn, Quote, QuotePin

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


# --- check-ins & streaks -----------------------------------------------------

def check_in(user_id: int, day: date | None = None) -> bool:
    """Record today's check-in. Returns False if already checked in."""
    day = day or date.today()
    if CheckIn.query.filter_by(user_id=user_id, date=day).first():
        return False
    db.session.add(CheckIn(user_id=user_id, date=day))
    db.session.commit()
    return True


def _streak_ending(dates: set[date], start: date) -> int:
    """Walk backwards from `start` counting the streak.

    Grace rule: one single missing day does not break the streak ("rest day"),
    at most one rest day per rolling 7 days. Rest days don't add to the count.
    """
    streak = 0
    day = start
    rest_days: list[date] = []
    while True:
        if day in dates:
            streak += 1
            day -= timedelta(days=1)
            continue
        # missing day: can we spend a rest day? (none used in the last 7 days)
        if any(abs((r - day).days) < 7 for r in rest_days):
            break
        # two missing days in a row is a real break, not a rest day
        if (day - timedelta(days=1)) not in dates:
            break
        rest_days.append(day)
        day -= timedelta(days=1)
    return streak


def streak_info(user_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    dates = {c.date for c in CheckIn.query.filter_by(user_id=user_id).all()}

    checked_today = today in dates
    # a morning visit before check-in shouldn't show zero: anchor on yesterday
    anchor = today if checked_today else today - timedelta(days=1)
    current = _streak_ending(dates, anchor)

    # longest streak ever (small data; walk each run start)
    longest = current
    for d in dates:
        if (d - timedelta(days=1)) not in dates:  # run can only start here
            run_end = d
            while run_end + timedelta(days=1) in dates or (
                run_end + timedelta(days=2) in dates
            ):
                run_end += timedelta(days=1)
            longest = max(longest, _streak_ending(dates, run_end))

    last7 = [(today - timedelta(days=i)) in dates for i in range(6, -1, -1)]
    return {
        "current": current,
        "longest": longest,
        "total": len(dates),
        "checked_today": checked_today,
        "last7": last7,
    }
