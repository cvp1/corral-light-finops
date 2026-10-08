"""One budgeted pass over every usage store (plan §6.5, Budget).

Newest files first, so the current month fills in first; the pass stops at
the budget and the next run carries on from the cursors. Format drift: a
source whose candidate lines parse below DRIFT_RATE this run (with at least
DRIFT_MIN of them) is frozen at its last facts until a new module version
is installed.
"""
import time

from finops import VERSION
from finops.ledger import Ledger
from finops.sources import billed, claude, codex, gemini, grok
from finops.sources.feed import Feed, note_logins
from finops.sources.lines import Tally

DRIFT_RATE = 0.9
DRIFT_MIN = 20


class ScanResult:
    def __init__(self):
        self.tallies = {}
        self.remaining_bytes = 0
        self.total_bytes = 0
        self.pending_files = 0
        self.frozen = []
        self.notes = []

    @property
    def complete(self):
        return self.pending_files == 0

    def done_pct(self):
        if self.total_bytes <= 0:
            return 100
        return int(100 * (self.total_bytes - self.remaining_bytes) / self.total_bytes)


def _frozen(ledger, source):
    s = ledger.source_state(source)
    if s and s["state"] == "format_changed":
        if s["version"] == VERSION:
            return True
        ledger.set_source_state(source, "ok", "unfrozen by a module update", time.time(),
                                VERSION)
    return False


def run(env, budget_s, ledger=None):
    """-> (ledger, feed, ScanResult). The caller closes the ledger."""
    deadline = time.monotonic() + budget_s
    ledger = ledger or Ledger(env.data)
    res = ScanResult()
    res.notes += ledger.notes
    feed = Feed(env.feed)
    ledger.begin()
    try:
        note_logins(ledger, feed, env.now())
        ledger.commit()
    except BaseException:
        ledger.rollback()
        raise
    billed.ingest(ledger, env.fetched, res.notes)
    t = res.tallies.setdefault("grok", Tally())
    if not _frozen(ledger, "grok"):
        grok.refresh(ledger, env.feed, t)
    work = []
    if not _frozen(ledger, "claude"):
        work += [("claude",) + w for w in claude.work(ledger, env.reads.get("claude-projects", []))]
    if not _frozen(ledger, "codex"):
        work += [("codex",) + w for w in codex.work(ledger, env.data,
                                                     env.reads.get("codex-sessions", []))]
    if not _frozen(ledger, "gemini"):
        work += [("gemini",) + w for w in gemini.work(ledger, env.data,
                                                       env.reads.get("gemini-store", []))]
    # Newest first: the current month fills in before old history.
    work.sort(key=lambda w: -w[1])
    res.total_bytes = _backfill_total(ledger) + sum(w[2] for w in work)
    left = list(work)
    for n, (src, _mtime, size, _path, runner) in enumerate(work):
        if n and time.monotonic() >= deadline:
            break                       # always some progress, even on a tiny budget
        tally = res.tallies.setdefault(src, Tally())
        status = runner(tally, deadline)
        if status != "partial":
            left.remove(next(w for w in left if w[4] is runner))
    res.pending_files = len(left)
    res.remaining_bytes = sum(w[2] for w in left)
    for src, tally in res.tallies.items():
        n = tally.ok + tally.bad
        if n >= DRIFT_MIN and tally.ok / n < DRIFT_RATE:
            note = (f"only {tally.ok:,} of {n:,} new records parsed; frozen at its last "
                    f"figures until FinOps is updated")
            ledger.begin()
            ledger.set_source_state(src, "format_changed", note, time.time(), VERSION)
            ledger.commit()
            res.frozen.append(src)
    return ledger, feed, res


def _backfill_total(ledger):
    """Bytes already read, so progress is a share of all history."""
    return ledger.one("SELECT COALESCE(SUM(offset), 0) FROM files WHERE source != 'gemini'") or 0
