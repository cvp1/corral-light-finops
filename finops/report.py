"""Everything the dialog and the CLI show, derived at read time from the
ledger, the feed, the config and the price list (plan §2.1, §6.4).

Metrics never mix: quota state, subscription commitment, vendor-computed
cost, and API-equivalent list cost are separate figures with their own
kinds. Nothing here writes.
"""
from finops import util
from finops.config import match_plan
from finops.sources import codex as codex_src

VENDORS = ("claude", "codex", "grok", "gemini")
LANE_TITLES = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "gemini": "Gemini"}
HOUR, DAY = 3600, 86400


# ── accounts ───────────────────────────────────────────────────────────────

def _claude_fps(ledger):
    return ledger.q("SELECT fp, first_seen, plan, tier FROM logins_seen WHERE lane='claude' "
                    "ORDER BY first_seen, fp")


def discover(ledger, feed):
    """{key: {vendor, key, vendor_plan, tier, source}} for every account
    the usage stores or the feed show."""
    out = {}
    for fp, _first, plan, tier in _claude_fps(ledger):
        vp = plan + (f"/{tier}" if tier else "") if plan else None
        out[f"claude:{fp}"] = {"vendor": "claude", "key": f"claude:{fp}", "plan": plan,
                               "tier": tier, "vendor_plan": vp, "source": "Claude login"}
    if not out and (ledger.one("SELECT 1 FROM claude_req LIMIT 1") or
                    ledger.one("SELECT 1 FROM claude_noreq LIMIT 1")):
        out["claude:local"] = {"vendor": "claude", "key": "claude:local", "plan": None,
                               "tier": None, "vendor_plan": None,
                               "source": "transcripts (no login facts in the feed)"}
    accts = {r[0] for r in ledger.q("SELECT DISTINCT account FROM codex_session")}
    accts |= {r[0] for r in ledger.q("SELECT DISTINCT account FROM codex_resp")}
    accts |= {r[0] for r in ledger.q("SELECT DISTINCT account FROM codex_plan")}
    for a in sorted(x or "unknown" for x in accts):
        plan = ledger.one("SELECT plan FROM codex_plan WHERE account=?", (a,))
        out[f"codex:{a}"] = {"vendor": "codex", "key": f"codex:{a}", "plan": plan, "tier": None,
                             "vendor_plan": plan, "source": "Codex rollouts"}
    grok_login = feed.login("grok") if feed else None
    if ledger.one("SELECT 1 FROM grok_session LIMIT 1") or (grok_login or {}).get("present"):
        out["grok:local"] = {"vendor": "grok", "key": "grok:local", "plan": None, "tier": None,
                             "vendor_plan": None, "source": "Grok usage reports"}
    if ledger.one("SELECT 1 FROM gemini_call LIMIT 1"):
        out["gemini:local"] = {"vendor": "gemini", "key": "gemini:local", "plan": None,
                               "tier": None, "vendor_plan": None, "source": "Antigravity store"}
    return out


def accounts(ledger, feed, cfg, plans):
    """Discovered accounts joined with the config. Each says whether the
    operator accepted it and what its monthly commitment is, with kind."""
    found = discover(ledger, feed)
    by_match = cfg.by_match()
    taken = {a["id"] for a in cfg.accounts}
    out = []
    from finops.config import new_account_id
    for key, d in found.items():
        c = by_match.get(key)
        cat = match_plan(plans, d["vendor"], d["plan"], d["tier"])
        a = dict(d, confirmed=c is not None, catalogue=cat)
        if c:
            a["id"] = c["id"]
            a["title"] = c.get("title") or c["id"]
        else:
            a["id"] = new_account_id(d["vendor"], key, taken)
            taken.add(a["id"])
            a["title"] = a["id"]
        a["commit"] = _commitment(c, d, cat)
        out.append(a)
    # Accounts the operator wrote that no store shows (yet): their typed
    # price still counts.
    for c in cfg.accounts:
        if c.get("match") in found:
            continue
        d = {"vendor": c["vendor"], "key": c.get("match") or f"config:{c['id']}",
             "plan": None, "tier": None, "vendor_plan": None, "source": "your config"}
        a = dict(d, confirmed=True, catalogue=None, id=c["id"], title=c.get("title") or c["id"])
        a["commit"] = _commitment(c, d, None)
        out.append(a)
    out.sort(key=lambda a: (VENDORS.index(a["vendor"]) if a["vendor"] in VENDORS else 9,
                            a["id"]))
    return out


def _commitment(c, d, cat):
    """{cents, kind, note} for one account's monthly subscription."""
    hint = None
    if cat:
        hint = {"cents": cat["usd_cents_month"], "kind": "list",
                "note": f"{cat.get('title', cat['id'])}: list price as of {cat.get('as_of')}, "
                        f"not confirmed"}
    if c and c.get("price_from") == "operator" and isinstance(c.get("usd_cents_month"), int):
        typed_for = c.get("price_for_plan")
        now_plan = d.get("vendor_plan")
        if now_plan and typed_for and now_plan != typed_for:
            out = dict(hint) if hint else {"cents": None, "kind": "unknown"}
            out["note"] = (f"plan changed from {typed_for} to {now_plan}; price not confirmed"
                           + (f" ({hint['note']})" if hint else ""))
            out["plan_changed"] = True
            return out
        return {"cents": c["usd_cents_month"], "kind": "declared",
                "note": f"typed in setup on {c.get('price_at') or 'an earlier day'}"}
    if hint:
        return hint
    if d["vendor"] in ("grok", "gemini"):
        return {"cents": None, "kind": "unknown", "note": "the vendor does not state a plan "
                "locally; type a price in setup if you pay for one"}
    return {"cents": None, "kind": "unknown",
            "note": "the vendor's plan is not known here, so no price is assumed"}


# ── usage facts ────────────────────────────────────────────────────────────

class Fact:
    __slots__ = ("vendor", "account", "ts", "model", "session", "tokens", "list_micros",
                 "vendor_micros", "dedup")

    def __init__(self, vendor, account, ts, model, session, tokens, list_micros,
                 vendor_micros=None, dedup=True):
        self.vendor, self.account, self.ts, self.model = vendor, account, ts, model
        self.session, self.tokens, self.list_micros = session, tokens, list_micros
        self.vendor_micros, self.dedup = vendor_micros, dedup


def _claude_owner(fps):
    """ts -> account key, by the login timeline."""
    if not fps:
        return lambda ts: "claude:local"
    marks = [(first, f"claude:{fp}") for fp, first, _p, _t in fps]

    def owner(ts):
        cur = marks[0][1]
        for first, key in marks:
            if first <= ts:
                cur = key
        return cur
    return owner


def facts(ledger, prices, since, until=None):
    """Every usage fact with since <= ts < until."""
    until = float("inf") if until is None else until
    owner = _claude_owner(_claude_fps(ledger))
    for table, dedup in (("claude_req", True), ("claude_noreq", False)):
        for ts, model, sid, i, w5, w1, cr, o in ledger.it(
                f"SELECT ts, model, session_id, input, cache_write_5m, cache_write_1h, "
                f"cache_read, output FROM {table} WHERE ts >= ? AND ts < ?", (since, until)):
            parts = {"input": i or 0, "cache_write_5m": w5 or 0, "cache_write_1h": w1 or 0,
                     "cache_read": cr or 0, "output": o or 0}
            yield Fact("claude", owner(ts), ts, model, sid, sum(parts.values()),
                       prices.cost(model, ts, parts), dedup=dedup)
    with_records = {r[0] for r in ledger.q("SELECT DISTINCT session_id FROM codex_resp "
                                           "WHERE session_id IS NOT NULL")}
    for ts, sid, acct, model, i, c, o, _r in ledger.it(
            "SELECT ts, session_id, account, model, input, cached, output, reasoning "
            "FROM codex_resp WHERE ts >= ? AND ts < ?", (since, until)):
        yield _codex_fact(prices, ts, sid, acct, model, i or 0, c or 0, o or 0)
    sessions = {}
    for row in ledger.q("SELECT session_id, ts, total, account, model, input, cached, output, "
                        "reasoning FROM codex_cum"):
        if row[0] not in with_records:
            sessions.setdefault(row[0], []).append(row[1:])
    for sid, rows in sessions.items():
        for ts, model, acct, i, c, o, _r in codex_src.deltas(rows):
            if since <= ts < until:
                yield _codex_fact(prices, ts, sid, acct, model, i, c, o)
    forks = {r[0]: r[1] for r in ledger.q("SELECT session_id, forked_ns FROM grok_session "
                                          "WHERE forked_ns IS NOT NULL")}
    for sid, ended, model, i, c, w, o, ticks in ledger.it(
            "SELECT session_id, ended_ns, model, input, cached, cache_write, output, ticks "
            "FROM grok_turn"):
        if ended is None:
            continue
        if sid in forks and ended <= forks[sid]:
            continue                                  # inherited from the parent
        ts = ended / 1e9
        if not since <= ts < until:
            continue
        i, c, w, o = i or 0, c or 0, w or 0, o or 0
        parts = {"input": max(i - c, 0), "cache_read": c, "cache_write_5m": w, "output": o}
        # A turn spans many model calls, so the base tier is used, never a
        # long-context tier chosen from the turn's summed input.
        yield Fact("grok", "grok:local", ts, model, sid, i + w + o,
                   prices.cost(model, ts, parts, input_tokens=0),
                   vendor_micros=None if ticks is None else util.ticks_to_micros(ticks))
    for cid, ts, model, u, c, o in ledger.it(
            "SELECT cascade_id, ts, model, uncached, cached, output FROM gemini_call "
            "WHERE ts >= ? AND ts < ?", (since, until)):
        parts = {"input": u or 0, "cache_read": c or 0, "output": o or 0}
        yield Fact("gemini", "gemini:local", ts, model, cid, sum(parts.values()),
                   prices.cost(model, ts, parts))


def _codex_fact(prices, ts, sid, acct, model, i, c, o):
    parts = {"input": max(i - c, 0), "cache_read": c, "output": o}
    return Fact("codex", f"codex:{acct or 'unknown'}", ts, model, sid, i + o,
                prices.cost(model, ts, parts, input_tokens=i))


class Totals:
    def __init__(self):
        self.tokens = 0
        self.list_micros = 0
        self.unpriced_tokens = 0
        self.unpriced_models = set()
        self.vendor_micros = None
        self.not_dedup = 0
        self.records = 0

    def add(self, f):
        self.records += 1
        self.tokens += f.tokens
        if f.list_micros is None:
            self.unpriced_tokens += f.tokens
            self.unpriced_models.add(f.model or "(no model id)")
        else:
            self.list_micros += f.list_micros
        if f.vendor_micros is not None:
            self.vendor_micros = (self.vendor_micros or 0) + f.vendor_micros
        if not f.dedup:
            self.not_dedup += 1


def totals_by(facts_iter, key):
    out = {}
    for f in facts_iter:
        out.setdefault(key(f), Totals()).add(f)
    return out


# ── quota (plan §6.5, Quota freshness) ────────────────────────────────────

CLAUDE_WINDOWS = {"five_hour": ("5 h", 5 * HOUR)}
CLAUDE_SHORTEST = 5 * HOUR


def _claude_window(name):
    if name in CLAUDE_WINDOWS:
        return CLAUDE_WINDOWS[name]
    if name.startswith("seven_day"):
        label = "weekly" + (" " + name[len("seven_day_"):].replace("_", " ")
                            if name != "seven_day" else "")
        return label, 7 * DAY
    return name.strip("_").replace("_", " ") or "unknown", None


def freshness(now, observed_at, resets_at, length_s, shortest_s):
    """'reset' | 'stale' | 'current', one rule for every vendor."""
    if resets_at is not None and resets_at <= now:
        return "reset"
    age = now - (observed_at or 0)
    if resets_at is None:
        return "stale" if age > shortest_s else "current"
    if length_s is not None and age > length_s:
        return "stale"
    if length_s is None and age > shortest_s:
        return "stale"
    return "current"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def quota(ledger, feed, now):
    """[window dict] for every vendor-reported window."""
    out = []
    for fp, acct in (feed.quota_accounts() if feed else {}).items():
        if not isinstance(acct, dict) or acct.get("lane") != "claude":
            continue
        for name, w in sorted((acct.get("windows") or {}).items()):
            if not isinstance(w, dict):
                continue
            label, length = _claude_window(name)
            carried = w.get("carried") if isinstance(w.get("carried"), dict) else {}
            obs = _num(w.get("observed_at")) or 0
            util_v = _num(w.get("utilization"))
            util_at = _num(carried.get("utilization")) or obs
            status_at = _num(carried.get("status")) or obs
            resets = _num(w.get("resets_at_s"))
            pct_state = freshness(now, util_at, resets, length, CLAUDE_SHORTEST)
            status_state = freshness(now, status_at, resets, length, CLAUDE_SHORTEST)
            status = w.get("status") if isinstance(w.get("status"), str) else None
            out.append({"vendor": "claude", "account": f"claude:{fp}", "window": name,
                        "label": f"Claude {label}",
                        "bp": None if util_v is None else int(round(util_v * 10000)),
                        "state": pct_state if util_v is not None else status_state,
                        "status": status if status_state == "current" else None,
                        "resets_at": resets, "observed_at": max(util_at, status_at) if
                        util_v is not None else status_at, "length_s": length})
    for acct, window, ts, bp, minutes, resets in ledger.q(
            "SELECT account, window, ts, used_bp, window_minutes, resets_at FROM codex_quota "
            "ORDER BY account, window_minutes"):
        length = minutes * 60 if minutes else None
        label = {300: "5 h", 10080: "weekly"}.get(minutes, f"{minutes} min" if minutes
                                                    else "window")
        out.append({"vendor": "codex", "account": f"codex:{acct}", "window": window,
                    "label": f"Codex {label}", "bp": bp,
                    "state": freshness(now, ts, resets, length, 5 * HOUR),
                    "status": None, "resets_at": resets, "observed_at": ts,
                    "length_s": length})
    return out


# ── sources ────────────────────────────────────────────────────────────────

def source_rows(ledger, feed, env):
    """[(lane, state, detail)] for the Sources list."""
    rows = []
    reads = env.reads

    def drift(src):
        s = ledger.source_state(src)
        return s if s and s["state"] == "format_changed" else None

    def files_seen(src):
        return ledger.one("SELECT COUNT(*) FROM files WHERE source=?", (src,)) or 0

    def line(lane, src, have_paths, n, unit, login, missing_text):
        d = drift(src)
        if d:
            rows.append((lane, "format changed", d["note"] or "frozen at its last facts"))
        elif n:
            rows.append((lane, "reported", f"{n:,} {unit} from {files_seen(src):,} files"))
        elif login:
            rows.append((lane, "not reported", "credentials found, usage not reported on disk"))
        elif have_paths:
            rows.append((lane, "no usage yet", "the usage store is there and empty"))
        else:
            rows.append((lane, "not found", missing_text))

    cl = feed.login("claude") if feed else None
    line("Claude", "claude", bool(reads.get("claude-projects")),
         (ledger.one("SELECT COUNT(*) FROM claude_req") or 0) +
         (ledger.one("SELECT COUNT(*) FROM claude_noreq") or 0), "requests",
         bool(cl and cl.get("present")), "no transcripts in view")
    co = feed.login("codex") if feed else None
    line("Codex", "codex", bool(reads.get("codex-sessions")),
         (ledger.one("SELECT COUNT(*) FROM codex_resp") or 0) +
         (ledger.one("SELECT COUNT(DISTINCT session_id) FROM codex_cum") or 0),
         "responses", bool(co and co.get("present")), "no rollouts in view")
    gr = feed.login("grok") if feed else None
    ns = ledger.one("SELECT COUNT(*) FROM grok_session") or 0
    stale = ledger.one("SELECT COUNT(*) FROM grok_session WHERE stale=1") or 0
    if drift("grok"):
        rows.append(("Grok", "format changed", drift("grok")["note"]))
    elif ns:
        rows.append(("Grok", "reported", f"{ns:,} sessions, cost computed by the Grok CLI"
                     + (f"; {stale} stale" if stale else "")))
    elif gr and gr.get("present"):
        rows.append(("Grok", "not reported", "credentials found; Light has not reported "
                     "usage yet (it runs `grok usage` before each collector run)"))
    else:
        rows.append(("Grok", "not found", "no Grok login and no reports"))
    line("Gemini", "gemini", bool(reads.get("gemini-store")),
         ledger.one("SELECT COUNT(*) FROM gemini_call") or 0, "model calls", False,
         "no Antigravity store in view")
    rows.append(("Ollama", "local", "runs on this machine; provider charge not measured"))
    return rows
