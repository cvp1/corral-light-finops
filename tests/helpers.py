"""Synthetic fixtures. Every record here is made up; none comes from a real
transcript."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True

from finops import util  # noqa: E402
from finops.config import Config, load_plans  # noqa: E402
from finops.prices import Prices  # noqa: E402
from finops import scan, view  # noqa: E402

SENTINEL = "SENTINEL-PROMPT-7f3a9c"


def ts(s):
    """'2026-10-07T12:00:00Z' -> epoch seconds."""
    return util.iso_s(s)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── Claude ─────────────────────────────────────────────────────────────────

def claude_line(rid, at, model="claude-opus-5-5", inp=10, out=5, cr=100, w5=0, w1=0,
                session="sess-a", text=SENTINEL, typ="assistant"):
    rec = {"type": typ, "timestamp": at, "sessionId": session, "uuid": "u-" + str(rid),
           "message": {"model": model, "id": "msg-" + str(rid), "role": "assistant",
                       "content": [{"type": "text", "text": text}],
                       "usage": {"input_tokens": inp, "output_tokens": out,
                                 "cache_read_input_tokens": cr,
                                 "cache_creation_input_tokens": w5 + w1,
                                 "cache_creation": {"ephemeral_5m_input_tokens": w5,
                                                    "ephemeral_1h_input_tokens": w1}}}}
    if rid is not None:
        rec["requestId"] = rid
    return json.dumps(rec, ensure_ascii=False)


def user_line(at, text=SENTINEL, session="sess-a"):
    return json.dumps({"type": "user", "timestamp": at, "sessionId": session,
                       "message": {"role": "user", "content": text}})


# ── Codex ──────────────────────────────────────────────────────────────────

def codex_meta(sid, at, account="acct-1"):
    return json.dumps({"timestamp": at, "type": "session_meta",
                       "payload": {"id": sid, "timestamp": at, "creator_account_id": account,
                                   "cwd": "/x", "base_instructions": SENTINEL}})


def codex_ctx(at, model="gpt-6-astra"):
    return json.dumps({"timestamp": at, "type": "turn_context", "payload": {"model": model}})


def _u(i, c, o, r=0):
    return {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o,
            "reasoning_output_tokens": r, "total_tokens": i + o}


def codex_record(rid, at, i, c, o, sid="s1"):
    return json.dumps({"timestamp": at, "type": "token_usage_record",
                       "payload": {"response_id": rid, "session_id": sid, "usage": _u(i, c, o)}})


def codex_count(at, i, c, o, rate_limits=None):
    p = {"type": "token_count", "info": {"total_token_usage": _u(i, c, o)}}
    if rate_limits is not None:
        p["rate_limits"] = rate_limits
    return json.dumps({"timestamp": at, "type": "event_msg", "payload": p})


def codex_message(at, text=SENTINEL):
    return json.dumps({"timestamp": at, "type": "response_item",
                       "payload": {"type": "message", "content": [{"text": text}]}})


def rate_limits(primary_pct=20.0, primary_reset=None, secondary_pct=5.0,
                secondary_reset=None, plan="plus"):
    return {"plan_type": plan,
            "primary": {"used_percent": primary_pct, "window_minutes": 300,
                        "resets_at": primary_reset},
            "secondary": {"used_percent": secondary_pct, "window_minutes": 10080,
                          "resets_at": secondary_reset}}


# ── Grok ───────────────────────────────────────────────────────────────────

def grok_report(sid, turns, parent=None, forked_at=None, stale=False):
    """turns: [(turnNumber, endedAt, input, output, ticks)]."""
    t = [{"turnNumber": n, "endedAt": at, "inputTokens": i, "cachedReadTokens": 0,
          "cacheCreationTokens": 0, "outputTokens": o, "reasoningTokens": 0,
          "costUsdTicks": ticks, "primaryModelId": "grok-4.7"} for n, at, i, o, ticks in turns]
    return {"schema": "corral-light.grok-usage/1", "sessionId": sid,
            "session": {"costUsdTicks": sum(x[4] for x in turns)}, "turns": t,
            "parent_session_id": parent, "forked_at": forked_at,
            "grok_version": "grok 1.0.46", "stale": stale}


# ── Gemini (protobuf by hand) ──────────────────────────────────────────────

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_int(f, v):
    return _varint(f << 3) + _varint(v)


def pb_bytes(f, b):
    return _varint((f << 3) | 2) + _varint(len(b)) + b


def gemini_db(path, cascade, calls, wal=False, gen=True):
    """calls: [(step, epoch_s, uncached, cached, thinking, visible, model or None)]."""
    if os.path.exists(path):
        os.unlink(path)
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE trajectory_meta (trajectory_id, cascade_id, trajectory_type, "
                "source)")
    con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, status, "
                "has_subtrajectory, metadata BLOB, error_details, permissions, task_details, "
                "render_info, step_payload BLOB, step_format)")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    con.execute("INSERT INTO trajectory_meta VALUES ('t', ?, 1, 1)", (cascade,))
    for gi, (step, at, unc, cac, think, vis, model) in enumerate(calls):
        usage = pb_int(2, unc) + pb_int(3, think + vis) + pb_int(5, cac) + \
            (pb_int(9, think) if think else b"") + pb_int(10, vis)
        created = pb_int(1, int(at)) + pb_int(2, 0)
        meta = pb_bytes(1, created) + pb_bytes(9, usage) + \
            pb_bytes(24, pb_bytes(8, b"gemini-3.8-flash-high"))
        con.execute("INSERT INTO steps (idx, step_type, metadata, step_payload) "
                    "VALUES (?, 15, ?, ?)", (step, meta, SENTINEL.encode()))
        if gen and model:
            inner = pb_bytes(4, usage) + pb_bytes(19, model.encode())
            data = pb_bytes(1, inner) + pb_bytes(2, _varint(step)) + pb_bytes(4, cascade.encode())
            con.execute("INSERT INTO gen_metadata VALUES (?,?,?)", (gi, data, len(data)))
    con.commit()
    return con          # left open when wal=True so the WAL is not checkpointed


# ── a whole host ───────────────────────────────────────────────────────────

class Host:
    """A temp layout like Light gives a module: reads, feed, data, config."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="finops-test-")
        self.claude = self.mk("claude-home/projects/p1")
        self.panes = self.mk("pane/config/projects/p1")
        self.codex = self.mk("codex/sessions/2026/10/07")
        self.gemini = self.mk("gemini/conversations")
        self.feed = self.mk("feed")
        self.data = self.mk("data")
        self.cfgdir = self.mk("config")
        self.now = ts("2026-10-07T20:00:00Z")
        self.tz = "UTC"

    def mk(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(p, exist_ok=True)
        return p

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self, feed=True, **kw):
        return util.Env(data=self.data, config=os.path.join(self.cfgdir, "config.toml"),
                        feed=self.feed if feed else None,
                        reads={"claude-projects": [os.path.dirname(self.claude),
                                                   os.path.dirname(self.panes)],
                               "codex-sessions": [os.path.join(self.root, "codex/sessions")],
                               "gemini-store": [self.gemini]},
                        tz=self.tz, now=kw.get("now", self.now))

    def write(self, path, lines, mode="w"):
        with open(path, mode, encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
        return path

    def feed_json(self, name, obj):
        p = os.path.join(self.feed, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(obj, f)

    def logins(self, claude_fp="fp-claude-1", plan="max", tier="default_claude_max_5x",
               grok=True):
        self.feed_json("logins.json", {"schema": "corral-light.module-feed/1", "lanes": {
            "claude": {"present": True, "plan": plan, "tier": tier, "fingerprint": claude_fp},
            "codex": {"present": True, "plan": "plus", "fingerprint": "fp-codex-feed"},
            "grok": {"present": grok, "auth_mode": "present" if grok else None}}})

    def grok(self, rep):
        self.feed_json(f"vendor/grok-usage/{rep['sessionId']}.json", rep)

    def run(self, budget=25, **kw):
        """One collector pass -> (snapshot, scan result, ledger-closed)."""
        env = self.env(**{k: v for k, v in kw.items() if k in ("feed", "now")})
        ledger, feed, res = scan.run(env, budget)
        try:
            snap = view.build(env, ledger, feed, Config.load(env.config), load_plans(),
                              Prices.load(), scan=res)
        finally:
            ledger.close()
        return snap, res

    def ledger(self):
        from finops.ledger import Ledger
        return Ledger(self.data)


class HostCase(unittest.TestCase):
    def setUp(self):
        self.h = Host()

    def tearDown(self):
        self.h.cleanup()


def tile(snap, label_start):
    for b in snap["view"]:
        if b["type"] == "tiles":
            for it in b["items"]:
                if it["label"].startswith(label_start):
                    return it
    return None


def table(snap, title_start):
    for b in snap["view"]:
        if b["type"] == "table" and b["title"].startswith(title_start):
            return b
    return None


def notes(snap):
    return [b["text"] for b in snap["view"] if b["type"] == "note"]


def totals(h, since=0):
    """{vendor: (tokens, list_micros, vendor_micros)} straight from the ledger."""
    from finops import report
    led = h.ledger()
    try:
        out = {}
        for k, t in report.totals_by(report.facts(led, Prices.load(), since),
                                     lambda f: f.vendor).items():
            out[k] = (t.tokens, t.list_micros, t.vendor_micros, t.unpriced_tokens)
        return out
    finally:
        led.close()
