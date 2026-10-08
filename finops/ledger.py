"""The ledger: SQLite in the module's data dir (plan §6.5).

It holds counts, model ids, session ids and timestamps; never prompt or
answer text. Every figure the dialog shows is derived from these tables and
the price list at read time, so the same history read in any order, with any
duplicates and with a crash between batches, gives the same totals.

Facts are keyed by the vendor's own identity for a record: Claude by
requestId (largest output count wins), Codex by response_id (or, for older
rollouts, the cumulative counter at a timestamp), Grok by (session, turn),
Gemini by (conversation, step).
"""
import os
import shutil
import sqlite3
import time

SCHEMA_VERSION = 1
FILE = "ledger.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, source TEXT NOT NULL, dev INTEGER, ino INTEGER,
  size INTEGER, mtime_ns INTEGER, offset INTEGER NOT NULL DEFAULT 0,
  lines_ok INTEGER NOT NULL DEFAULT 0, lines_bad INTEGER NOT NULL DEFAULT 0,
  lines_long INTEGER NOT NULL DEFAULT 0, state TEXT);
CREATE TABLE IF NOT EXISTS claude_req (
  request_id TEXT PRIMARY KEY, ts REAL NOT NULL, model TEXT, session_id TEXT,
  input INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER,
  cache_read INTEGER, output INTEGER, path TEXT);
CREATE TABLE IF NOT EXISTS claude_noreq (
  path TEXT NOT NULL, off INTEGER NOT NULL, ts REAL NOT NULL, model TEXT,
  session_id TEXT, input INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER,
  cache_read INTEGER, output INTEGER, PRIMARY KEY (path, off));
CREATE TABLE IF NOT EXISTS codex_resp (
  response_id TEXT PRIMARY KEY, session_id TEXT, account TEXT, ts REAL NOT NULL,
  model TEXT, input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER);
CREATE TABLE IF NOT EXISTS codex_cum (
  session_id TEXT NOT NULL, ts REAL NOT NULL, total INTEGER NOT NULL,
  account TEXT, model TEXT, input INTEGER, cached INTEGER, output INTEGER,
  reasoning INTEGER, PRIMARY KEY (session_id, ts, total));
CREATE TABLE IF NOT EXISTS codex_session (
  session_id TEXT PRIMARY KEY, account TEXT, started REAL, mid_history INTEGER);
CREATE TABLE IF NOT EXISTS codex_quota (
  account TEXT NOT NULL, window TEXT NOT NULL, ts REAL NOT NULL,
  used_bp INTEGER, window_minutes INTEGER, resets_at REAL, PRIMARY KEY (account, window));
CREATE TABLE IF NOT EXISTS codex_plan (
  account TEXT PRIMARY KEY, plan TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grok_turn (
  session_id TEXT NOT NULL, turn INTEGER NOT NULL, ended_ns INTEGER, model TEXT,
  input INTEGER, cached INTEGER, cache_write INTEGER, output INTEGER,
  reasoning INTEGER, ticks INTEGER, PRIMARY KEY (session_id, turn));
CREATE TABLE IF NOT EXISTS grok_session (
  session_id TEXT PRIMARY KEY, parent TEXT, forked_ns INTEGER, sig TEXT,
  version TEXT, stale INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS gemini_call (
  cascade_id TEXT NOT NULL, step INTEGER NOT NULL, ts REAL NOT NULL, model TEXT,
  uncached INTEGER, cached INTEGER, output INTEGER, thinking INTEGER,
  PRIMARY KEY (cascade_id, step));
CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY, state TEXT NOT NULL, note TEXT, at REAL,
  module_version TEXT);
CREATE TABLE IF NOT EXISTS logins_seen (
  lane TEXT NOT NULL, fp TEXT NOT NULL, first_seen REAL NOT NULL,
  plan TEXT, tier TEXT, PRIMARY KEY (lane, fp));
CREATE TABLE IF NOT EXISTS billed_day (
  account TEXT NOT NULL, day TEXT NOT NULL, currency TEXT NOT NULL,
  amount TEXT NOT NULL, PRIMARY KEY (account, day, currency));
CREATE TABLE IF NOT EXISTS billed_fetch (
  account TEXT PRIMARY KEY, vendor TEXT NOT NULL, org_id TEXT, org_name TEXT,
  fetched_at TEXT NOT NULL, range_start TEXT, range_end TEXT, notes TEXT);
CREATE INDEX IF NOT EXISTS claude_req_ts ON claude_req(ts);
CREATE INDEX IF NOT EXISTS codex_resp_ts ON codex_resp(ts);
CREATE INDEX IF NOT EXISTS gemini_ts ON gemini_call(ts);
"""

# Migrations from version N to N+1, each one SQL script run in one
# transaction after the old ledger is copied aside. Version 1 is the first.
MIGRATIONS = {}


class Ledger:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, FILE)
        self.notes = []                   # what opening did (rebuilt, migrated)
        self.db = self._open()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _open(self):
        os.makedirs(self.data_dir, exist_ok=True)
        db = None
        try:
            db = self._connect()
            ver = _version(db)
        except sqlite3.DatabaseError:
            if db is not None:
                db.close()
            self._set_aside("unreadable")
            self.notes.append("the ledger could not be read; it is kept aside and history "
                              "is read again")
            db = self._connect()
            ver = None
        if ver is not None and ver > SCHEMA_VERSION:
            # Written by newer code (a rollback): never opened for writing.
            db.close()
            self._set_aside(f"v{ver}")
            self.notes.append(f"the ledger was written by a newer FinOps (schema {ver}); "
                              f"it is kept aside and history is read again")
            db = self._connect()
            ver = None
        if ver is not None and ver < SCHEMA_VERSION:
            db.close()
            self._migrate(ver)
            db = self._connect()
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        return db

    def _set_aside(self, tag):
        stamp = time.strftime("%Y%m%dT%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.replace(p, f"{self.path}.{tag}.{stamp}{suffix}")

    def _migrate(self, ver):
        """Copy aside, then migrate in one transaction; the copy is what
        `module rollback` restores with the previous generation."""
        backup = f"{self.path}.v{ver}.bak"
        src = self._connect()
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            while ver < SCHEMA_VERSION:
                for stmt in _split(MIGRATIONS[ver]):
                    db.execute(stmt)
                ver += 1
            db.execute("UPDATE meta SET value=? WHERE key='schema'", (str(ver),))
            db.execute("COMMIT")
            self.notes.append(f"the ledger was migrated to schema {ver}; the old copy is "
                              f"{os.path.basename(backup)}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    # ── transactions ────────────────────────────────────────────────────

    def begin(self):
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.db.execute("COMMIT")

    def rollback(self):
        try:
            self.db.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def close(self):
        self.db.close()

    def q(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def it(self, sql, args=()):
        """Rows one at a time, on their own cursor: big tables are never
        held in memory whole."""
        return self.db.cursor().execute(sql, args)

    def one(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return r[0] if r else None

    # ── per-file cursors ───────────────────────────────────────────────

    def cursor(self, path):
        r = self.db.execute("SELECT dev, ino, size, mtime_ns, offset, lines_ok, lines_bad, "
                            "lines_long, state FROM files WHERE path=?", (path,)).fetchone()
        if not r:
            return None
        keys = ("dev", "ino", "size", "mtime_ns", "offset", "lines_ok", "lines_bad",
                "lines_long", "state")
        return dict(zip(keys, r))

    def save_cursor(self, path, source, st, offset, ok, bad, long_, state=None):
        self.db.execute(
            "INSERT INTO files (path, source, dev, ino, size, mtime_ns, offset, lines_ok, "
            "lines_bad, lines_long, state) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET source=excluded.source, dev=excluded.dev, "
            "ino=excluded.ino, size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "offset=excluded.offset, lines_ok=excluded.lines_ok, lines_bad=excluded.lines_bad, "
            "lines_long=excluded.lines_long, state=excluded.state",
            (path, source, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, offset, ok, bad,
             long_, state))

    # ── source state (format drift) ────────────────────────────────────

    def source_state(self, source):
        r = self.db.execute("SELECT state, note, at, module_version FROM source_state "
                            "WHERE source=?", (source,)).fetchone()
        return None if not r else {"state": r[0], "note": r[1], "at": r[2], "version": r[3]}

    def set_source_state(self, source, state, note, at, version):
        self.db.execute("INSERT INTO source_state VALUES (?,?,?,?,?) ON CONFLICT(source) "
                        "DO UPDATE SET state=excluded.state, note=excluded.note, "
                        "at=excluded.at, module_version=excluded.module_version",
                        (source, state, note, at, version))


def _version(db):
    try:
        r = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return None
        raise
    return int(r[0]) if r else None


def _split(script):
    return [s.strip() for s in script.split(";") if s.strip()]


def remove_copies(data_dir):
    """For tests and `--purge`-like resets: the ledger and its set-aside copies."""
    for name in os.listdir(data_dir):
        if name.startswith(FILE):
            p = os.path.join(data_dir, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.unlink(p)
