"""Codex rollouts (`codex-sessions`).

- `token_usage_record`: one per model response, keyed by response_id; a
  repeat is ignored. The first choice wherever a session has them.
- `token_count`: the cumulative total, kept per (session, time, total) and
  turned into deltas only at read time, sorted, for sessions with no
  per-response records (older rollouts). Its `rate_limits` give the plan
  and the vendor's quota windows, newest by the event's own time.
- `session_meta`: the session id and the account, hashed with the module's
  salt; the raw account id is never stored.
- `turn_context`: the model for the records that follow it in the file.
"""
import json
import os

from finops.sources.lines import int_or_none, pending, read_file
from finops.util import account_hash, iso_s

SOURCE = "codex"
_WANT = (b'"session_meta"', b'"turn_context"', b'"token_usage_record"', b'"token_count"')


def files(roots):
    out = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for fn in filenames:
                if fn.endswith(".jsonl") and not os.path.islink(os.path.join(dirpath, fn)):
                    out.append(os.path.join(dirpath, fn))
    return out


def _usage(u):
    if not isinstance(u, dict):
        return None
    vals = {k: int_or_none(u.get(k)) for k in ("input_tokens", "cached_input_tokens",
                                                "output_tokens", "reasoning_output_tokens",
                                                "total_tokens")}
    if vals["input_tokens"] is None or vals["output_tokens"] is None:
        return None
    for k in ("cached_input_tokens", "reasoning_output_tokens"):
        vals[k] = vals[k] or 0
    if vals["total_tokens"] is None:
        vals["total_tokens"] = vals["input_tokens"] + vals["output_tokens"]
    return vals


def _window_key(minutes):
    return f"{minutes}m" if isinstance(minutes, int) and minutes > 0 else "_unknown"


def _handler(ledger, data_dir, path):
    def handle(line, offset, state):
        if not any(w in line for w in _WANT):
            return None
        try:
            d = json.loads(line)
        except ValueError:
            return "bad"
        if not isinstance(d, dict):
            return None
        typ = d.get("type")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        ts = iso_s(d.get("timestamp"))
        if typ == "session_meta":
            sid = p.get("id") or p.get("session_id")
            if not isinstance(sid, str) or not sid or ts is None:
                return "bad"
            acct = account_hash(data_dir, "codex", p.get("creator_account_id"))
            state["session"], state["account"] = sid[:120], acct
            ledger.db.execute("INSERT INTO codex_session VALUES (?,?,?,0) ON CONFLICT"
                              "(session_id) DO UPDATE SET account=COALESCE(excluded.account, "
                              "codex_session.account), started=MIN(codex_session.started, "
                              "excluded.started), mid_history=0",
                              (sid[:120], acct, ts))
            return "ok"
        if typ == "turn_context":
            m = p.get("model")
            if isinstance(m, str) and len(m) <= 120:
                state["model"] = m
            return "ok"
        if typ == "token_usage_record":
            rid = p.get("response_id")
            u = _usage(p.get("usage"))
            if not isinstance(rid, str) or not rid or u is None or ts is None:
                return "bad"
            sid = p.get("session_id") if isinstance(p.get("session_id"), str) else \
                state.get("session")
            ledger.db.execute(
                "INSERT OR IGNORE INTO codex_resp VALUES (?,?,?,?,?,?,?,?,?)",
                (rid[:200], (sid or "")[:120] or None, state.get("account"), ts,
                 state.get("model"), u["input_tokens"], u["cached_input_tokens"],
                 u["output_tokens"], u["reasoning_output_tokens"]))
            return "ok"
        if typ == "event_msg" and p.get("type") == "token_count":
            if ts is None:
                return "bad"
            sid = state.get("session")
            if not sid:
                # Counts with no session header before them: history before
                # this point was not read.
                sid = "file:" + os.path.basename(path)[:100]
                state["session"] = sid
                ledger.db.execute("INSERT OR IGNORE INTO codex_session VALUES (?,?,?,1)",
                                  (sid, state.get("account"), ts))
            info = p.get("info")
            if isinstance(info, dict):
                u = _usage(info.get("total_token_usage"))
                if u is None:
                    return "bad"
                ledger.db.execute(
                    "INSERT OR IGNORE INTO codex_cum VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, ts, u["total_tokens"], state.get("account"), state.get("model"),
                     u["input_tokens"], u["cached_input_tokens"], u["output_tokens"],
                     u["reasoning_output_tokens"]))
            rl = p.get("rate_limits")
            if isinstance(rl, dict):
                _rate_limits(ledger, state.get("account") or "unknown", rl, ts)
            return "ok"
        return None
    return handle


def _rate_limits(ledger, acct, rl, ts):
    plan = rl.get("plan_type")
    if isinstance(plan, str) and len(plan) <= 40:
        ledger.db.execute("INSERT INTO codex_plan VALUES (?,?,?) ON CONFLICT(account) DO "
                          "UPDATE SET plan=excluded.plan, ts=excluded.ts "
                          "WHERE excluded.ts >= codex_plan.ts", (acct, plan, ts))
    for k in ("primary", "secondary"):
        w = rl.get(k)
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            pct = None
        minutes = int_or_none(w.get("window_minutes"))
        resets = w.get("resets_at")
        resets = float(resets) if isinstance(resets, (int, float)) and \
            not isinstance(resets, bool) and resets > 0 else None
        if resets is not None and resets > 1e11:
            resets /= 1000.0
        bp = None if pct is None else int(round(pct * 100))
        ledger.db.execute(
            "INSERT INTO codex_quota VALUES (?,?,?,?,?,?) ON CONFLICT(account, window) DO "
            "UPDATE SET ts=excluded.ts, used_bp=excluded.used_bp, "
            "window_minutes=excluded.window_minutes, resets_at=excluded.resets_at "
            "WHERE excluded.ts > codex_quota.ts",
            (acct, _window_key(minutes), ts, bp, minutes, resets))


def work(ledger, data_dir, roots):
    out = []
    for p in files(roots):
        st, need = pending(ledger, p)
        if st is not None and need:
            c = ledger.cursor(p)
            todo = st.st_size - (c["offset"] if c and c["ino"] == st.st_ino else 0)
            out.append((st.st_mtime, max(todo, 0), p, _runner(ledger, data_dir, p)))
    return out


def _runner(ledger, data_dir, path):
    def run(tally, deadline):
        return read_file(ledger, path, SOURCE, _handler(ledger, data_dir, path), tally,
                         deadline)
    return run


def deltas(rows):
    """Cumulative token_count rows of ONE session, any order and with
    duplicates -> [(ts, model, account, d_input, d_cached, d_output,
    d_reasoning)]. Sorted by (time, total); equal totals are dropped; a lower
    total is a counter reset and counts its own total from zero."""
    rows = sorted(set(rows), key=lambda r: (r[0], r[1]))
    out, prev = [], None
    for ts, total, account, model, i, c, o, r in rows:
        if prev is not None and total == prev[0]:
            continue
        if prev is None or total < prev[0]:
            base = (0, 0, 0, 0)
        else:
            base = prev[1]
        out.append((ts, model, account, max(i - base[0], 0), max(c - base[1], 0),
                    max(o - base[2], 0), max(r - base[3], 0)))
        prev = (total, (i, c, o, r))
    return out
