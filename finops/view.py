"""The snapshot (corral-light.module/1): typed blocks only, every value
text, every figure labelled with its kind (plan §4.5, §6.4).

Null is unreported, never zero: a figure with no source says so.
"""
import hashlib
import re

from finops import report, util
from finops.util import usd, usd_cents

SCHEMA = "corral-light.module/1"
DOCS = "https://github.com/cvp1/corral-light-finops#how-the-figures-are-made"


def _level_for(bp, state):
    if state != "current" or bp is None:
        return "info"
    if bp >= 9000:
        return "bad"
    if bp >= 7500:
        return "warn"
    return "ok"


def _pct(bp):
    return f"{bp / 100:.0f}%" if bp is not None else None


def quota_text(w, now, tz):
    """(value, note, level) for one quota window, by the freshness rule."""
    if w["state"] == "reset":
        return "reset since last report", "no newer report from the vendor yet", "info"
    pct = _pct(w["bp"])
    value = f"{pct} used" if pct else "no percent reported"
    note = []
    if w["status"]:
        note.append({"allowed": "allowed", "allowed_warning": "near its limit",
                     "rejected": "limit reached"}.get(w["status"], w["status"]))
    if w["resets_at"]:
        note.append("resets " + util.short_when(w["resets_at"], now, tz))
    if w["state"] == "stale":
        note.append(f"stale: reported {util.age_text(now - w['observed_at'])} ago")
        level = "info"
    else:
        level = _level_for(w["bp"], w["state"])
        if w["status"] == "rejected":
            level = "bad"
        elif w["status"] == "allowed_warning" and level == "ok":
            level = "warn"
    return value, "; ".join(note), level


_NOTICE_BAD = re.compile(r"[^a-z0-9._-]")
SOURCES = ("claude", "codex", "grok", "gemini")


def notice_id(*parts):
    """A stable notice id in the core's alphabet (plan §4.7): lowercased,
    other characters mapped to '-', and past 64 characters the first 55
    plus a hash of the whole."""
    whole = _NOTICE_BAD.sub("-", ".".join(str(p) for p in parts).lower())
    if len(whole) <= 64:
        return whole
    return whole[:55] + "-" + hashlib.sha256(whole.encode()).hexdigest()[:8]


def notices(quota, ledger, now, tz):
    """Rail notices: only what the operator can act on, from figures the
    snapshot already shows, at the same levels as their tiles."""
    out = []
    for w in quota:
        value, note, level = quota_text(w, now, tz)
        if level not in ("warn", "bad"):
            continue
        pct = _pct(w["bp"])
        if pct:
            title = f"{w['label']} {pct} used"
        else:
            title = f"{w['label']}: " + ("limit reached" if w["status"] == "rejected"
                                         else "near its limit")
        exp = w["resets_at"]
        if exp is None and w["observed_at"] and w.get("length_s"):
            exp = w["observed_at"] + w["length_s"]
        n = {"id": notice_id("quota", w["account"], w["window"]), "level": level,
             "title": title[:80], "text": note[:300]}
        if exp:
            n["expires_at"] = util.utc_iso(exp)
        out.append(n)
    for src in SOURCES:
        st = ledger.source_state(src)
        if st and st["state"] == "format_changed":
            out.append({"id": notice_id("source", src, "frozen"), "level": "warn",
                        "title": f"{report.LANE_TITLES[src]} figures frozen",
                        "text": "its record format changed, so its figures stopped at "
                                "their last values; a module update brings them back"})
    rank = {"bad": 0, "warn": 1}
    out.sort(key=lambda n: (rank[n["level"]], n["id"]))
    return out


def build(env, ledger, feed, cfg, plans, prices, scan=None, now=None):
    now = env.now() if now is None else now
    tz = util.zone(_tz_name(cfg, env, feed))
    gen = util.utc_iso(now)
    m0 = util.month_start(now, tz)
    month = util.month_label(now, tz)
    accts = report.accounts(ledger, feed, cfg, plans)
    by_key = {a["key"]: a for a in accts}
    per_acct, per_vendor, n_month = {}, {}, 0
    for f in report.facts(ledger, prices, m0):          # streamed, never listed
        n_month += 1
        per_acct.setdefault(f.account, report.Totals()).add(f)
        per_vendor.setdefault(f.vendor, report.Totals()).add(f)
    quota = report.quota(ledger, feed, now)
    view, notes = [], []

    # ── tiles ──
    declared = [a for a in accts if a["commit"]["kind"] == "declared"]
    listed = [a for a in accts if a["commit"]["kind"] == "list" and a["commit"]["cents"]]
    tiles = []
    if declared:
        tiles.append({"label": "Committed", "kind": "declared",
                      "value": usd_cents(sum(a["commit"]["cents"] for a in declared)) + " / mo",
                      "note": "prices you typed" + (
                          f"; plus {usd_cents(sum(a['commit']['cents'] for a in listed))} in "
                          f"unconfirmed list prices" if listed else ""),
                      "fresh_at": gen, "level": "info"})
    elif listed:
        tiles.append({"label": "Committed", "kind": "list",
                      "value": usd_cents(sum(a["commit"]["cents"] for a in listed)) + " / mo",
                      "note": "unconfirmed list prices for the plans your vendors state; "
                              "type yours in `corral-light finops setup`",
                      "fresh_at": gen, "level": "info"})
    else:
        tiles.append({"label": "Committed", "kind": "unknown", "value": "unreported",
                      "note": "no plan price is known; type yours in "
                              "`corral-light finops setup`", "fresh_at": gen, "level": "info"})
    for w in sorted(quota, key=lambda w: (w["state"] != "current", -(w["bp"] or -1)))[:4]:
        value, note, level = quota_text(w, now, tz)
        tiles.append({"label": w["label"], "value": value, "kind": "vendor", "note": note,
                      "fresh_at": util.utc_iso(w["observed_at"]) if w["observed_at"] else "",
                      "level": level})
    if not quota:
        tiles.append({"label": "Quota", "value": "unreported", "kind": "unknown",
                      "note": "no vendor has reported a quota window yet", "fresh_at": gen,
                      "level": "info"})
    g = per_vendor.get("grok")
    if g is not None and g.vendor_micros is not None:
        tiles.append({"label": f"Grok, {month}", "value": usd(g.vendor_micros), "kind": "vendor",
                      "note": "computed by the Grok CLI (not a bill)", "fresh_at": gen,
                      "level": "info"})
    total_list = sum(t.list_micros for k, t in per_vendor.items())
    unpriced = sum(t.unpriced_tokens for t in per_vendor.values())
    tiles.append({"label": f"API-equivalent list cost, {month}",
                  "value": usd(total_list) if n_month else "unreported",
                  "kind": "list" if n_month else "unknown",
                  "note": "what this usage would cost at API list prices; not a bill, "
                          "never added to a plan" + (
                              f"; {util.tokens(unpriced)} tokens unpriced" if unpriced else ""),
                  "fresh_at": gen, "level": "info"})
    view.append({"type": "tiles", "items": tiles})

    # ── meters ──
    for w in quota:
        if w["bp"] is None or w["state"] == "reset":
            continue
        value, note, level = quota_text(w, now, tz)
        view.append({"type": "meter", "label": w["label"], "pct": w["bp"] / 100,
                     "kind": "vendor", "level": level, "note": f"{value}; {note}" if note
                     else value})

    # ── by account ──
    rows = []
    for a in accts:
        t = per_acct.get(a["key"])
        c = a["commit"]
        commit = (usd_cents(c["cents"]) + (" typed" if c["kind"] == "declared" else " list")
                  if c["cents"] is not None else "unreported")
        if c.get("plan_changed"):
            commit += " (plan changed)"
        plan = a["catalogue"]["title"] if a.get("catalogue") else (a.get("vendor_plan") or
                                                                    "not stated")
        rows.append([
            a["title"] + ("" if a["confirmed"] else " (proposed)"),
            report.LANE_TITLES.get(a["vendor"], a["vendor"]), plan, commit,
            util.tokens(t.tokens) if t else "0",
            _list_text(t) if t else "$0.00",
            usd(t.vendor_micros) if t and t.vendor_micros is not None else "-",
            a["source"]])
    view.append({"type": "table", "title": f"By account, {month}",
                 "columns": ["Account", "Lane", "Plan", "Committed / mo", "Tokens",
                             "API-equivalent list cost", "Vendor-computed cost", "Source"],
                 "rows": rows})

    # ── sources ──
    view.append({"type": "table", "title": "Sources",
                 "columns": ["Lane", "State", "Detail"],
                 "rows": [list(r) for r in report.source_rows(ledger, feed, env)]})

    # ── most used this week ──
    view.append(_most_used(report.facts(ledger, prices, now - 7 * 86400), feed))

    # ── notes ──
    proposed = [a for a in accts if not a["confirmed"]]
    if proposed:
        notes.append(f"{len(proposed)} proposed account(s). Accept them, and type what you "
                     f"pay, with: corral-light finops setup")
    models = sorted(set().union(*(t.unpriced_models for t in per_vendor.values()))) \
        if per_vendor else []
    if models:
        notes.append("Unpriced this month (counted, never priced at $0): " +
                     ", ".join(models[:8]) + (" …" if len(models) > 8 else ""))
    nd = sum(t.not_dedup for t in per_vendor.values())
    if nd:
        notes.append(f"{nd:,} Claude records carry no request id and are counted without "
                     f"deduplication.")
    if scan is not None:
        notes += scan.notes
        for src in scan.frozen:
            notes.append(f"{src}: the record format changed; its figures are frozen.")
    if cfg.error:
        notes.append(cfg.error)
    if feed is None or not feed.present:
        notes.append("Light's feed is not in view, so logins, Claude quota and pane names "
                     "are unreported.")
    if not env.sandboxed:
        notes.append("This module runs unsandboxed on this host (acknowledged at install).")
    notes.append(f"This host only. Month boundaries in {_tz_name(cfg, env, feed) or 'UTC'}.")
    for n in notes:
        view.append({"type": "note", "text": n, "level": "info"})
    view.append({"type": "link", "label": "How these figures are made", "url": DOCS})

    snap = {"schema": SCHEMA, "ok": True, "generated_at": gen, "error": None, "view": view}
    if getattr(cfg, "notices", "on") != "off":
        snap["notices"] = notices(quota, ledger, now, tz)
    if scan is not None and not scan.complete:
        snap["progress"] = {"phase": "backfill", "done_pct": scan.done_pct(),
                            "note": f"reading history: {scan.pending_files} files to go"}
    return snap


def _list_text(t):
    """A list cost that never shows unpriced usage as $0."""
    if t.unpriced_tokens and t.unpriced_tokens >= t.tokens:
        return "unpriced"
    return usd(t.list_micros) + (" + unpriced" if t.unpriced_tokens else "")


def _tz_name(cfg, env, feed):
    if cfg.timezone and cfg.timezone != "local":
        return cfg.timezone
    return env.tz or (feed.host_tz() if feed else None)


def _most_used(week, feed):
    panes = {p.get("acp_session"): p for p in (feed.panes() if feed else [])
             if isinstance(p.get("acp_session"), str)}
    by_session = report.totals_by((f for f in week if f.session), lambda f: f.session)
    rows, groups = [], {}
    for sid, t in by_session.items():
        p = panes.get(sid)
        if p is None:
            continue
        lane = report.LANE_TITLES.get(p.get("agent"), p.get("agent") or "?")
        name = f"{p.get('title') or lane} · {str(p.get('id'))[:6]}"
        origin = p.get("role") or p.get("origin") or "human"
        rows.append((t.list_micros, t.vendor_micros or 0, name, lane, origin, t))
        for gk in ([f"worktree {p['worktree_id']}"] if p.get("worktree_id") else []) + \
                (["all consult panes"] if p.get("origin") == "consult" else []):
            g = groups.setdefault(gk, report.Totals())
            g.list_micros += t.list_micros
            g.tokens += t.tokens
            g.unpriced_tokens += t.unpriced_tokens
            if t.vendor_micros is not None:
                g.vendor_micros = (g.vendor_micros or 0) + t.vendor_micros
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]))
    out = [[name, lane, origin, util.tokens(t.tokens),
            _list_text(t), usd(t.vendor_micros) if t.vendor_micros is not None else "-"]
           for _l, _v, name, lane, origin, t in rows[:12]]
    for gk, t in sorted(groups.items(), key=lambda kv: -kv[1].list_micros):
        out.append([gk, "-", "group", util.tokens(t.tokens), _list_text(t),
                    usd(t.vendor_micros) if t.vendor_micros is not None else "-"])
    return {"type": "table", "title": "Most used this week (panes Light knows)",
            "columns": ["Pane", "Lane", "Origin", "Tokens", "API-equivalent list cost",
                        "Vendor-computed cost"],
            "rows": out or [["No pane usage in the last 7 days", "", "", "", "", ""]]}


def error_snapshot(message, now_s):
    return {"schema": SCHEMA, "ok": False, "generated_at": util.utc_iso(now_s),
            "error": message[:480], "view": [{"type": "note", "text": message[:480],
                                              "level": "bad"}]}
