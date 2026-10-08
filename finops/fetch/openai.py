"""OpenAI organization Costs API (daily). `amount.value` is in whole
currency units, `currency` lowercase ISO 4217."""
import calendar
import urllib.parse
from datetime import datetime, timezone

from finops.fetch.common import Days, dec, result

BASE = "https://api.openai.com"


def _ts(d):
    return calendar.timegm(d.timetuple())


def fetch(client, start, end, params=None):
    h = {}                       # Light's fetch proxy adds the credential
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
            # Anything malformed fails the fetch: a run that skipped it would
            # report an empty day and replace a complete one.
            if not isinstance(b, dict) or not isinstance(b.get("results"), list):
                raise ValueError("a costs bucket without a results list")
            day = datetime.fromtimestamp(int(b["start_time"]), timezone.utc).strftime("%Y-%m-%d")
            for r in b["results"]:
                amt = r.get("amount") if isinstance(r, dict) else None
                if not isinstance(amt, dict) or not amt.get("currency"):
                    raise ValueError("a costs result without an amount and currency")
                days.add(day, amt["currency"], dec(amt.get("value")))
        if not doc.get("has_more"):
            break
        page = doc.get("next_page")
        if not isinstance(page, str) or not page:
            raise ValueError("the costs list said has_more but gave no next_page")
    else:
        raise ValueError("the costs list did not end after 100 pages")
    return result("openai", start, end, days, {})
