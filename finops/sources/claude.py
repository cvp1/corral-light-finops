"""Claude Code transcripts (`claude-projects`): one record per content block
of a model response, all with the request's usage and a growing output
count. Keyed by requestId; the record with the largest output count wins
(ties: the earliest timestamp), so the order files are read in and any
copies across homes do not change the result. `<synthetic>` records are
skipped. Records with no requestId are kept apart, per file and offset,
and marked "not deduplicated".

Only the usage numbers, the model id, the session id and the time are
kept; the line is dropped as soon as it is parsed.
"""
import json
import os

from finops.sources.lines import int_or_none, read_file, pending
from finops.util import iso_s

SOURCE = "claude"


def files(roots):
    out = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for fn in filenames:
                if fn.endswith(".jsonl"):
                    p = os.path.join(dirpath, fn)
                    if not os.path.islink(p):
                        out.append(p)
    return out


def parse(line):
    """-> None (not a usage record), 'bad', 'skip' or a fact dict."""
    if b'"assistant"' not in line or b'"usage"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return "bad"
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    m = d.get("message")
    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
        return None
    model = m.get("model")
    if model == "<synthetic>":
        return "skip"
    u = m["usage"]
    inp, out = int_or_none(u.get("input_tokens")), int_or_none(u.get("output_tokens"))
    ts = iso_s(d.get("timestamp"))
    if inp is None or out is None or ts is None:
        return "bad"
    cr = int_or_none(u.get("cache_read_input_tokens")) or 0
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    if cc is not None:
        w5 = int_or_none(cc.get("ephemeral_5m_input_tokens")) or 0
        w1 = int_or_none(cc.get("ephemeral_1h_input_tokens")) or 0
    else:
        w5, w1 = int_or_none(u.get("cache_creation_input_tokens")) or 0, 0
    rid = d.get("requestId")
    sid = d.get("sessionId")
    return {"request_id": rid if isinstance(rid, str) and 0 < len(rid) <= 200 else None,
            "ts": ts, "model": model if isinstance(model, str) and len(model) <= 120 else None,
            "session_id": sid if isinstance(sid, str) and len(sid) <= 120 else None,
            "input": inp, "cache_write_5m": w5, "cache_write_1h": w1,
            "cache_read": cr, "output": out}


def _handler(ledger, path):
    def handle(line, offset, state):
        f = parse(line)
        if f is None:
            return None
        if f in ("bad", "skip"):
            return "bad" if f == "bad" else "ok"
        args = (f["ts"], f["model"], f["session_id"], f["input"], f["cache_write_5m"],
                f["cache_write_1h"], f["cache_read"], f["output"])
        if f["request_id"]:
            ledger.db.execute(
                "INSERT INTO claude_req (request_id, ts, model, session_id, input, "
                "cache_write_5m, cache_write_1h, cache_read, output, path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                "ts=excluded.ts, model=excluded.model, session_id=excluded.session_id, "
                "input=excluded.input, cache_write_5m=excluded.cache_write_5m, "
                "cache_write_1h=excluded.cache_write_1h, cache_read=excluded.cache_read, "
                "output=excluded.output, path=excluded.path "
                "WHERE excluded.output > claude_req.output OR "
                "(excluded.output = claude_req.output AND excluded.ts < claude_req.ts)",
                (f["request_id"],) + args + (path,))
        else:
            ledger.db.execute(
                "INSERT OR REPLACE INTO claude_noreq (path, off, ts, model, session_id, input, "
                "cache_write_5m, cache_write_1h, cache_read, output) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (path, offset) + args)
        return "ok"
    return handle


def _reset(ledger):
    def on_reset(path):
        ledger.db.execute("DELETE FROM claude_noreq WHERE path=?", (path,))
    return on_reset


def work(ledger, roots):
    """[(mtime, size_to_read, path, run)] for files with something new."""
    out = []
    for p in files(roots):
        st, need = pending(ledger, p)
        if st is not None and need:
            c = ledger.cursor(p)
            todo = st.st_size - (c["offset"] if c and c["ino"] == st.st_ino else 0)
            out.append((st.st_mtime, max(todo, 0), p, _runner(ledger, p)))
    return out


def _runner(ledger, path):
    def run(tally, deadline):
        return read_file(ledger, path, SOURCE, _handler(ledger, path), tally, deadline,
                         on_reset=_reset(ledger))
    return run
