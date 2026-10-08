"""Shared pieces: the date window and the result document."""
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

RESULT_SCHEMA = "finops.billed/1"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CUR_RE = re.compile(r"^[A-Z]{3}$")


def window(now):
    """(start, end): the first day of last month and tomorrow, UTC dates;
    end is exclusive."""
    today = datetime.fromtimestamp(now, timezone.utc).date()
    first = today.replace(day=1)
    start = (first - timedelta(days=1)).replace(day=1)
    return start, today + timedelta(days=1)


def dec(v, scale=None):
    """A vendor amount (string or number) as Decimal; `scale` divides it
    (Anthropic reports cents)."""
    if isinstance(v, bool) or v is None:
        raise ValueError("no amount")
    try:
        d = Decimal(str(v)) if not isinstance(v, Decimal) else v
    except InvalidOperation:
        raise ValueError(f"not an amount: {str(v)[:40]!r}") from None
    if not d.is_finite():
        raise ValueError("not a finite amount")
    return d / scale if scale else d


class Days:
    """Per day, per currency, a Decimal sum."""

    def __init__(self):
        self.d = {}

    def add(self, day, currency, amount):
        if isinstance(day, (date, datetime)):
            day = day.strftime("%Y-%m-%d")
        cur = str(currency or "").upper()
        if not DAY_RE.match(str(day)) or not CUR_RE.match(cur):
            raise ValueError(f"bad day or currency: {str(day)[:20]!r} {cur[:8]!r}")
        per = self.d.setdefault(day, {})
        per[cur] = per.get(cur, Decimal(0)) + amount

    def doc(self):
        return {day: {c: _plain(v) for c, v in sorted(cs.items())}
                for day, cs in sorted(self.d.items())}


def _plain(d):
    s = format(d.normalize(), "f") if d else "0"
    return s


def result(vendor, start, end, days, org=None, notes=()):
    return {"schema": RESULT_SCHEMA, "vendor": vendor,
            "org": org or {}, "range": {"start": start.isoformat(), "end": end.isoformat()},
            "days": days.doc(), "notes": list(notes)}


def day_of(ts_text):
    """'2026-10-07T00:00:00Z' or '…+00:00' -> '2026-10-07' (UTC)."""
    t = str(ts_text).replace("Z", "+00:00")
    return datetime.fromisoformat(t).astimezone(timezone.utc).strftime("%Y-%m-%d")
