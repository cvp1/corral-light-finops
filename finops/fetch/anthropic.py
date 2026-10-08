"""Anthropic Admin API: organization cost report (daily), and the org's
name. `amount` is a string in the lowest currency unit (cents for USD)."""
import urllib.parse

from finops.fetch.common import Days, dec, day_of, result

BASE = "https://api.anthropic.com"
VERSION = "2023-06-01"
CENTS = 100


def fetch(client, key, start, end, params=None):
    h = {"x-api-key": key, "anthropic-version": VERSION}
    org = {}
    try:
        me = client.json("GET", f"{BASE}/v1/organizations/me", h)
        if isinstance(me, dict):
            org = {"id": str(me.get("id") or "")[:80], "name": str(me.get("name") or "")[:80]}
    except Exception:  # noqa: BLE001 — the name is a nicety; costs are the point
        org = {}
    days = Days()
    q = {"starting_at": f"{start.isoformat()}T00:00:00Z",
         "ending_at": f"{end.isoformat()}T00:00:00Z", "bucket_width": "1d", "limit": "31"}
    page = None
    for _ in range(100):
        qq = dict(q, **({"page": page} if page else {}))
        doc = client.json("GET", f"{BASE}/v1/organizations/cost_report?"
                          + urllib.parse.urlencode(qq), h)
        for b in _list(doc, "data"):
            day = day_of(b["starting_at"])
            for r in _list(b, "results"):
                days.add(day, r.get("currency") or "USD", dec(r.get("amount"), CENTS))
        if not doc.get("has_more"):
            break
        page = doc.get("next_page")
        if not isinstance(page, str) or not page:
            raise ValueError("the cost report said has_more but gave no next_page")
    else:
        raise ValueError("the cost report did not end after 100 pages")
    return result("anthropic", start, end, days, org,
                  notes=["Priority Tier costs are not in Anthropic's cost report"])


def _list(doc, key):
    v = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(v, list):
        raise ValueError(f"the response has no {key} list")
    return [x for x in v if isinstance(x, dict)]
