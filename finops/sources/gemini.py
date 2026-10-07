"""Gemini through Antigravity (`gemini-store`): one SQLite file per
conversation, named by its cascade id, in WAL mode.

The store is bound read-only and SQLite cannot open a WAL database there
without writing its shared-memory file, so a changed conversation is copied
(database and WAL) into the module's data dir, read, and the copy deleted.

Usage per model call is a protobuf message at `steps.metadata` field 9
(Phase 0, docs/finops-phase0.md): 2 uncached input, 5 cached input, 3 output
total, 9 thinking, 10 visible output. A call is (cascade id, step index).
The model id comes from the `gen_metadata` record that lists the step
(field 1.19), else the step's alias (24.8). Canary on every call: output
equals thinking plus visible. Undocumented format: a canary failure rate
over the drift threshold freezes the source.
"""
import os
import re
import shutil
import sqlite3

from finops import protobuf as pb

SOURCE = "gemini"
_CASCADE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,119}$")


def files(roots):
    out = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for fn in names:
            p = os.path.join(root, fn)
            if fn.endswith(".db") and _CASCADE.match(fn[:-3]) and os.path.isfile(p) \
                    and not os.path.islink(p):
                out.append(p)
    return out


def _sig(path):
    parts = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(path + suffix)
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def _model(s):
    if not isinstance(s, (bytes, bytearray)):
        return None
    try:
        t = bytes(s).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return t if _MODEL.match(t) else None


def _ts(msg):
    if not msg:
        return None
    d = pb.ints(msg)
    s = d.get(1)
    return None if not s else s + d.get(2, 0) / 1e9


def read_db(path, scratch):
    """-> (cascade_id, [call dicts], ok, bad). Raises sqlite3.Error/OSError."""
    os.makedirs(scratch, exist_ok=True)
    base = os.path.join(scratch, "copy.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(base + suffix)
        except OSError:
            pass
    try:
        shutil.copyfile(path, base)
        if os.path.exists(path + "-wal"):
            shutil.copyfile(path + "-wal", base + "-wal")
        con = sqlite3.connect(base)
        try:
            row = con.execute("SELECT cascade_id FROM trajectory_meta").fetchone()
            cascade = row[0] if row and isinstance(row[0], str) else \
                os.path.basename(path)[:-3]
            models = {}
            for (data,) in con.execute("SELECT data FROM gen_metadata"):
                try:
                    top = pb.fields(data)
                    inner = pb.sub(top, 1) or []
                    m = _model(pb.first(inner, 19, 2))
                    steps = pb.first(top, 2, 2)
                    idxs = pb.packed_varints(steps) if steps else \
                        [v for f, w, v in top if f == 2 and w == 0]
                except pb.Malformed:
                    continue
                if m and idxs:
                    models[idxs[0]] = m
            calls, ok, bad = [], 0, 0
            for idx, meta in con.execute("SELECT idx, metadata FROM steps "
                                         "WHERE metadata IS NOT NULL"):
                try:
                    fs = pb.fields(meta)
                except pb.Malformed:
                    continue
                u = pb.sub(fs, 9)
                if u is None:
                    continue
                n = pb.ints(u)
                out_total, thinking, visible = n.get(3), n.get(9, 0), n.get(10)
                ts = _ts(pb.sub(fs, 1))
                if out_total is None or visible is None or ts is None or \
                        out_total != thinking + visible:
                    bad += 1
                    continue
                ok += 1
                alias = pb.sub(fs, 24)
                model = models.get(idx) or (_model(pb.first(alias, 8, 2)) if alias else None)
                calls.append({"step": idx, "ts": ts, "model": model,
                              "uncached": n.get(2, 0), "cached": n.get(5, 0),
                              "output": out_total, "thinking": thinking})
            return cascade[:64], calls, ok, bad
        finally:
            con.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(base + suffix)
            except OSError:
                pass


class _St:
    def __init__(self, path):
        st = os.stat(path)
        self.st_dev, self.st_ino, self.st_size, self.st_mtime_ns = \
            st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def work(ledger, data_dir, roots):
    out = []
    for p in files(roots):
        sig = _sig(p)
        c = ledger.cursor(p)
        if c is not None and c["state"] == sig:
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        size = st.st_size + (os.path.getsize(p + "-wal") if os.path.exists(p + "-wal") else 0)
        out.append((st.st_mtime, size, p, _runner(ledger, data_dir, p, sig, size)))
    return out


def _runner(ledger, data_dir, path, sig, size):
    def run(tally, deadline):
        try:
            cascade, calls, ok, bad = read_db(path, os.path.join(data_dir, "tmp", "gemini"))
        except (sqlite3.Error, OSError):
            tally.bad += 1
            return "gone"
        ledger.begin()
        try:
            for c in calls:
                ledger.db.execute("INSERT OR REPLACE INTO gemini_call VALUES (?,?,?,?,?,?,?,?)",
                                  (cascade, c["step"], c["ts"], c["model"], c["uncached"],
                                   c["cached"], c["output"], c["thinking"]))
            ledger.save_cursor(path, SOURCE, _St(path), 0, ok, bad, 0, sig)
            ledger.commit()
        except BaseException:
            ledger.rollback()
            raise
        tally.ok += ok
        tally.bad += bad
        tally.bytes += size
        tally.files += 1
        return "done"
    return run
