"""xAI Management API: the team's daily spend. The team comes from the
management key's own validation. The `usd` value's unit is not stated in
xAI's reference; its example reads as dollars, and the result says so."""
import re
import urllib.parse

from finops.fetch.common import Days, dec, day_of, result

BASE = "https://management-api.x.ai"


def fetch(client, key, start, end, params=None):
    h = {"Authorization": f"Bearer {key}"}
    v = client.json("GET", f"{BASE}/auth/management-keys/validation", h)
    if not isinstance(v, dict):
        raise ValueError("the key validation response is not an object")
    # A team-scoped key names its team; an older answer without `scope`
    # still carries the (deprecated) teamId. Any other scope is refused:
    # xAI documents no way to list an organization's teams.
    if v.get("scope") == "SCOPE_TEAM":
        team = v.get("scopeId")
    elif "scope" not in v:
        team = v.get("teamId")
    else:
        team = None
    if not isinstance(team, str) or not re.match(r"^[A-Za-z0-9_-]{1,80}$", team):
        raise ValueError("this management key is not scoped to one team; FinOps reads "
                         "team-scoped keys only")
    days = Days()
    # One request per month at most: the series is dense, one point per day.
    body = {"analyticsRequest": {
        "timeRange": {"startTime": f"{start.isoformat()} 00:00:00",
                      "endTime": f"{end.isoformat()} 00:00:00", "timezone": "Etc/GMT"},
        "timeUnit": "TIME_UNIT_DAY",
        "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
        "groupBy": ["description"], "filters": []}}
    doc = client.json("POST", f"{BASE}/v1/billing/teams/{urllib.parse.quote(team, safe='')}/usage",
                      h, body=body)
    series = doc.get("timeSeries") if isinstance(doc, dict) else None
    if not isinstance(series, list):
        raise ValueError("the usage response has no timeSeries list")
    if doc.get("limitReached"):
        raise ValueError("xAI reported limitReached: the series is incomplete")
    for s in series:
        for p in (s.get("dataPoints") or []) if isinstance(s, dict) else []:
            vals = p.get("values") if isinstance(p, dict) else None
            if isinstance(vals, list) and vals and vals[0] is not None:
                day = day_of(p["timestamp"])
                if start.isoformat() <= day < end.isoformat():
                    days.add(day, "USD", dec(vals[0]))
    return result("xai", start, end, days, {"id": team[:80], "name": ""},
                  notes=["xAI's reference does not state the unit of its usd value; "
                         "FinOps reads it as dollars, as in xAI's example"])
