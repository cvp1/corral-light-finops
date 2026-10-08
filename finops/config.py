"""Operator config and the plan catalogue (plan §6.3).

The config lives where Light says (CORRAL_MODULE_CONFIG, a file in a folder
only `setup` may write), in the TOML subset Light's own reader takes:
strings, integers, comments and `[[account]]` tables.

    timezone = "local"
    notices = "off"                   # optional: no rail notices (default "on")

    [[account]]
    id = "claude-3be27e"
    vendor = "claude"
    kind = "subscription"
    match = "claude:3be27e…"          # the discovered account it stands for
    vendor_plan = "max/default_claude_max_5x"
    vendor_plan_at = "2026-10-07"
    plan = "claude-max-5x"            # catalogue id, if one matched
    plan_from = "catalogue"
    usd_cents_month = 10000           # only ever typed by the operator
    price_from = "operator"
    price_at = "2026-10-07"
    price_for_plan = "max/default_claude_max_5x"

A price is `declared` only when price_from is "operator" and the vendor
still states the plan it was typed for; otherwise the line falls back to
its catalogue hint (`list`) and says the plan changed.
"""
import os
import re

from finops import tomlmini
from finops.util import atomic_write

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANS_PATH = os.path.join(HERE, "data", "plans.toml")
ACCOUNT_KEYS = ("id", "vendor", "kind", "match", "title", "vendor_plan", "vendor_plan_at",
                "plan", "plan_from", "usd_cents_month", "price_from", "price_at",
                "price_for_plan", "renews_day", "accepted_at")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class ConfigError(ValueError):
    pass


def load_plans(path=PLANS_PATH):
    with open(path, encoding="utf-8") as f:
        doc = tomlmini.loads_strict(f.read())
    plans = doc.get("plan", [])
    for p in plans:
        if not isinstance(p.get("usd_cents_month"), int) or not isinstance(p.get("id"), str):
            raise ConfigError("plans.toml: every plan needs id and usd_cents_month")
    return plans


def match_plan(plans, vendor, vendor_plan, tier=None):
    """The one catalogue plan for what the vendor states, or None (no
    guess: zero or several matches give None)."""
    if not vendor_plan:
        return None
    hits = []
    for p in plans:
        if p.get("vendor") != vendor or p.get("vendor_plan") != vendor_plan:
            continue
        if p.get("tier") and (not tier or p["tier"] not in tier):
            continue
        hits.append(p)
    if len(hits) > 1:
        tiered = [p for p in hits if p.get("tier")]
        hits = tiered if len(tiered) == 1 else hits
    return hits[0] if len(hits) == 1 else None


class Config:
    def __init__(self, path, timezone="local", accounts=None, error=None, notices="on"):
        self.path = path
        self.timezone = timezone
        self.notices = notices
        self.accounts = accounts or []
        self.error = error

    @classmethod
    def load(cls, path):
        if not path or not os.path.exists(path):
            return cls(path)
        try:
            with open(path, encoding="utf-8") as f:
                doc = tomlmini.loads_strict(f.read())
        except (OSError, ValueError) as e:
            return cls(path, error=f"the FinOps config could not be read: {e}")
        accounts = []
        for a in doc.get("account", []):
            a = {k: v for k, v in a.items() if k in ACCOUNT_KEYS}
            if isinstance(a.get("id"), str) and isinstance(a.get("vendor"), str):
                accounts.append(a)
        tz = doc.get("timezone", "local")
        # Rail notices (plan §4.7): on unless the operator wrote "off".
        notices = "off" if doc.get("notices") == "off" else "on"
        return cls(path, timezone=tz if isinstance(tz, str) else "local", accounts=accounts,
                   notices=notices)

    def by_match(self):
        return {a["match"]: a for a in self.accounts if isinstance(a.get("match"), str)}

    def dumps(self):
        lines = ["# FinOps for Corral Light. Written by `corral-light finops setup`;",
                 "# safe to edit by hand (strings, whole numbers, comments only).",
                 "# usd_cents_month is a price you typed; it is never filled in for you.",
                 "", f"timezone = {tomlmini.basic(self.timezone)}"]
        if self.notices == "off":
            lines.append('notices = "off"')
        for a in self.accounts:
            lines += ["", "[[account]]"]
            for k in ACCOUNT_KEYS:
                v = a.get(k)
                if v is None:
                    continue
                if isinstance(v, bool) or not isinstance(v, (int, str)):
                    raise ConfigError(f"account {a.get('id')}: {k} must be text or a number")
                lines.append(f"{k} = {v}" if isinstance(v, int) else
                             f"{k} = {tomlmini.basic(v)}")
        return "\n".join(lines) + "\n"

    def save(self):
        if not self.path:
            raise ConfigError("no config path (CORRAL_MODULE_CONFIG is not set)")
        text = self.dumps()
        tomlmini.loads_strict(text)            # what we write, we can read
        atomic_write(self.path, text)


def new_account_id(vendor, key, taken):
    tail = re.sub(r"[^a-z0-9]", "", key.split(":", 1)[-1].lower())[:6] or "local"
    base = f"{vendor}-{tail}"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    if not _ID.match(out):
        out = f"{vendor}-{len(taken) + 1}"
    return out
