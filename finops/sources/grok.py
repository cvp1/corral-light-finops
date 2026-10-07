"""Grok's own usage reports, run by Light (vendor report `grok-usage`) and
read from the feed at <feed>/vendor/grok-usage/<session>.json.

Each report is the vendor CLI's complete account of one session, so it
replaces that session's turns. Turns are kept per (session, turn number),
dated by the turn's own end time, with cost as integer ticks (10^10 per
USD), never floats. A forked session's report repeats its parent's turns;
the feed carries `forked_at`, and only turns that ended after it count
(applied at read time). Session totals are never summed.
"""
import json
import os
import re

from finops.sources.lines import int_or_none
from finops.util import iso_ns

SOURCE = "grok"
SCHEMA = "corral-light.grok-usage/1"
_SID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_REPORT = 2 << 20


def report_dir(feed):
    return os.path.join(feed, "vendor", "grok-usage") if feed else None


def files(feed):
    d = report_dir(feed)
    try:
        names = sorted(os.listdir(d)) if d else []
    except OSError:
        return []
    return [os.path.join(d, n) for n in names
            if n.endswith(".json") and not n.startswith(".") and _SID.match(n[:-5])]


def parse(raw, sid):
    """-> dict or None. Only known numeric fields and ids are kept."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("sessionId") != sid:
        return None
    turns = []
    for t in doc.get("turns") or []:
        if not isinstance(t, dict):
            return None
        n = int_or_none(t.get("turnNumber"))
        if n is None:
            return None
        model = t.get("primaryModelId")
        turns.append({"turn": n, "ended_ns": iso_ns(t.get("endedAt")),
                      "model": model if isinstance(model, str) and len(model) <= 120 else None,
                      "input": int_or_none(t.get("inputTokens")),
                      "cached": int_or_none(t.get("cachedReadTokens")),
                      "cache_write": int_or_none(t.get("cacheCreationTokens")),
                      "output": int_or_none(t.get("outputTokens")),
                      "reasoning": int_or_none(t.get("reasoningTokens")),
                      "ticks": int_or_none(t.get("costUsdTicks"))})
    parent = doc.get("parent_session_id")
    version = doc.get("grok_version")
    return {"turns": turns, "stale": doc.get("stale") is True,
            "parent": parent if isinstance(parent, str) and len(parent) <= 120 else None,
            "forked_ns": iso_ns(doc.get("forked_at")),
            "version": version if isinstance(version, str) and len(version) <= 120 else None}


def refresh(ledger, feed, tally):
    """Read every changed report. Cheap: reports are small and few."""
    for p in files(feed):
        sid = os.path.basename(p)[:-5]
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig = f"{st.st_size}:{st.st_mtime_ns}"
        if ledger.one("SELECT sig FROM grok_session WHERE session_id=?", (sid,)) == sig:
            continue
        if st.st_size > MAX_REPORT:
            tally.bad += 1
            continue
        try:
            with open(p, "rb") as f:
                raw = f.read(MAX_REPORT + 1)
        except OSError:
            continue
        rep = parse(raw, sid)
        if rep is None:
            # Light writes {"stale": true} without turns when a call failed;
            # anything else unreadable counts against the source.
            try:
                stale = json.loads(raw).get("stale") is True
            except (ValueError, AttributeError):
                stale = False
            if stale:
                ledger.db.execute("UPDATE grok_session SET stale=1 WHERE session_id=?", (sid,))
            else:
                tally.bad += 1
            continue
        ledger.begin()
        try:
            if rep["stale"] and not rep["turns"]:
                ledger.db.execute("INSERT INTO grok_session (session_id, sig, stale) "
                                  "VALUES (?,?,1) ON CONFLICT(session_id) DO UPDATE SET "
                                  "stale=1, sig=excluded.sig", (sid, sig))
            else:
                ledger.db.execute("DELETE FROM grok_turn WHERE session_id=?", (sid,))
                for t in rep["turns"]:
                    ledger.db.execute(
                        "INSERT OR REPLACE INTO grok_turn VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, t["turn"], t["ended_ns"], t["model"], t["input"], t["cached"],
                         t["cache_write"], t["output"], t["reasoning"], t["ticks"]))
                ledger.db.execute(
                    "INSERT INTO grok_session VALUES (?,?,?,?,?,?) ON CONFLICT(session_id) "
                    "DO UPDATE SET parent=excluded.parent, forked_ns=excluded.forked_ns, "
                    "sig=excluded.sig, version=excluded.version, stale=excluded.stale",
                    (sid, rep["parent"], rep["forked_ns"], sig, rep["version"],
                     1 if rep["stale"] else 0))
            ledger.commit()
        except BaseException:
            ledger.rollback()
            raise
        tally.ok += len(rep["turns"]) or 1
        tally.files += 1
