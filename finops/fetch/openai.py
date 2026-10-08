"""OpenAI organization Costs API (daily). `amount.value` is in whole
currency units, `currency` lowercase ISO 4217."""
import calendar
import urllib.parse
from datetime import datetime, timezone

from finops.fetch.common import Days, dec, result

BASE = "https://api.openai.com"


def _ts(d):
    return calendar.timegm(d.timetuple())


def fetch(client, key, start, end, params=None):
    h = {"Authorization": f"Bearer {key}"}
    days = Days()
    q = {"start_time": str(_ts(start)), "end_time": str(_ts(end)), "bucket_width": "1d",
         "limit": "31"}
    page = None
    for _ in range(100):
        qq = dict(q, **({"page": page} if page else {}))
        doc = client.json("GET", f"{BASE}/v1/organization/costs?" + urllib.parse.urlencode(qq), h)
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, list):
            raise ValueError("the response has no data list")
        for b in data:
            if not isinstance(b, dict):
                continue
            day = datetime.fromtimestamp(int(b["start_time"]), timezone.utc).strftime("%Y-%m-%d")
            for r in b.get("results") or []:
                amt = r.get("amount") if isinstance(r, dict) else None
                if isinstance(amt, dict):
                    days.add(day, amt.get("currency") or "usd", dec(amt.get("value")))
        if not doc.get("has_more"):
            break
        page = doc.get("next_page")
        if not isinstance(page, str) or not page:
            raise ValueError("the costs list said has_more but gave no next_page")
    else:
        raise ValueError("the costs list did not end after 100 pages")
    return result("openai", start, end, days, {})
