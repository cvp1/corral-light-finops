"""Light's module feed (`light-feed`, module-feed/v1): panes, quota,
logins, host. Read-only; the module never writes there. A missing or
unreadable file is None, which the view shows as unreported."""
import json
import os

MAX_FEED_FILE = 16 << 20


def _load(feed, name):
    if not feed:
        return None
    p = os.path.join(feed, name)
    try:
        if os.path.getsize(p) > MAX_FEED_FILE:
            return None
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


class Feed:
    def __init__(self, feed_dir):
        self.dir = feed_dir
        self.panes_doc = _load(feed_dir, "panes.json")
        self.quota_doc = _load(feed_dir, "quota.json")
        self.logins_doc = _load(feed_dir, "logins.json")
        self.host_doc = _load(feed_dir, "host.json")

    @property
    def present(self):
        return any(d is not None for d in (self.panes_doc, self.quota_doc,
                                           self.logins_doc, self.host_doc))

    def panes(self):
        rows = (self.panes_doc or {}).get("panes")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def login(self, lane):
        lanes = (self.logins_doc or {}).get("lanes")
        v = lanes.get(lane) if isinstance(lanes, dict) else None
        return v if isinstance(v, dict) else None

    def quota_accounts(self):
        a = (self.quota_doc or {}).get("accounts")
        return a if isinstance(a, dict) else {}

    def host_tz(self):
        tz = (self.host_doc or {}).get("timezone")
        name = tz.get("name") if isinstance(tz, dict) else None
        return name if isinstance(name, str) else None


def note_logins(ledger, feed, now):
    """Remember when each Claude login fingerprint was first seen, so usage
    after a login change belongs to the new account. The first one ever
    seen owns all earlier history (transcripts do not name an account)."""
    c = feed.login("claude")
    fp = c.get("fingerprint") if c else None
    if not isinstance(fp, str) or not fp:
        return
    plan = c.get("plan") if isinstance(c.get("plan"), str) else None
    tier = c.get("tier") if isinstance(c.get("tier"), str) else None
    known = ledger.one("SELECT COUNT(*) FROM logins_seen WHERE lane='claude'")
    first = 0.0 if not known else now
    ledger.db.execute("INSERT INTO logins_seen VALUES ('claude',?,?,?,?) ON CONFLICT(lane, fp) "
                      "DO UPDATE SET plan=excluded.plan, tier=excluded.tier",
                      (fp, first, plan, tier))
