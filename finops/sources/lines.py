"""Incremental reading of append-only JSONL files (plan §6.5, Cursors).

Per file: device, inode, size, and the offset just past the last complete
line. A read stops at the last newline, so a partial last line (including a
split multi-byte character) waits for the next run. A shrunk file or a new
inode is read again from the start. A line over LONG_LINE bytes is skipped
and counted. Each chunk is one transaction together with its cursor, so a
crash between chunks loses nothing and repeats nothing that matters (every
fact is keyed by the vendor's record id).
"""
import json
import os
import time

LONG_LINE = 4 << 20
CHUNK = 8 << 20


class _Seen:
    """A stat whose size is how far this file has been read: saved after a
    chunk, so a run cut short is never mistaken for a finished file."""

    def __init__(self, st, size):
        self.st_dev, self.st_ino, self.st_mtime_ns = st.st_dev, st.st_ino, st.st_mtime_ns
        self.st_size = size


class Tally:
    """Candidate lines this run, per source: what parsed and what did not."""

    def __init__(self):
        self.ok = 0
        self.bad = 0
        self.long = 0
        self.bytes = 0
        self.files = 0

    def rate(self):
        n = self.ok + self.bad
        return None if n == 0 else self.ok / n


def pending(ledger, path):
    """(stat, needs_read) for one file; stat None when it is gone."""
    try:
        st = os.stat(path)
    except OSError:
        return None, False
    c = ledger.cursor(path)
    if c is None:
        return st, st.st_size > 0
    if (c["dev"], c["ino"]) != (st.st_dev, st.st_ino) or st.st_size < c["offset"]:
        return st, True
    if st.st_size == c["size"] and st.st_mtime_ns == c["mtime_ns"]:
        return st, False
    return st, st.st_size > c["offset"]


def read_file(ledger, path, source, handle, tally, deadline, on_reset=None):
    """Read what is new in `path`. handle(line_bytes, offset, state) ->
    'ok' | 'bad' | None (not a candidate). `state` is a per-file dict kept
    with the cursor. -> 'done' | 'partial' | 'gone'."""
    try:
        f = open(path, "rb")
    except OSError:
        return "gone"
    with f:
        st = os.fstat(f.fileno())
        c = ledger.cursor(path)
        fresh = c is None or (c["dev"], c["ino"]) != (st.st_dev, st.st_ino) \
            or st.st_size < c["offset"]
        if fresh:
            offset, ok, bad, long_, state = 0, 0, 0, 0, {}
        else:
            offset, ok, bad, long_ = c["offset"], c["lines_ok"], c["lines_bad"], c["lines_long"]
            state = _load_state(c["state"])
        first = True
        while True:
            if not first and time.monotonic() >= deadline:
                return "partial"
            first = False
            f.seek(offset)
            buf = f.read(CHUNK)
            if not buf:
                break
            end = buf.rfind(b"\n")
            if end < 0:
                if len(buf) < CHUNK:
                    break                       # a partial last line: next run
                # One line longer than a chunk: skip it whole.
                skip = _skip_long(f, offset + len(buf))
                if skip is None:
                    break
                ledger.begin()
                try:
                    if fresh and on_reset:
                        on_reset(path)
                        fresh = False
                    long_ += 1
                    tally.long += 1
                    offset = skip
                    ledger.save_cursor(path, source, _Seen(st, offset), offset, ok, bad, long_,
                                       json.dumps(state))
                    ledger.commit()
                except BaseException:
                    ledger.rollback()
                    raise
                continue
            ledger.begin()
            try:
                if fresh and on_reset:
                    on_reset(path)
                    fresh = False
                pos = 0
                body = buf[:end + 1]
                while pos < len(body):
                    nl = body.index(b"\n", pos)
                    line = body[pos:nl]
                    if len(line) > LONG_LINE:
                        long_ += 1
                        tally.long += 1
                    elif line.strip():
                        r = handle(line, offset + pos, state)
                        if r == "ok":
                            ok += 1
                            tally.ok += 1
                        elif r == "bad":
                            bad += 1
                            tally.bad += 1
                    pos = nl + 1
                offset += end + 1
                tally.bytes += end + 1
                ledger.save_cursor(path, source, _Seen(st, offset), offset, ok, bad, long_,
                                   json.dumps(state))
                ledger.commit()
            except BaseException:
                ledger.rollback()
                raise
        # Nothing new, or only a partial line: remember the size we saw.
        ledger.begin()
        try:
            if fresh and on_reset:
                on_reset(path)
            ledger.save_cursor(path, source, st, offset, ok, bad, long_, json.dumps(state))
            ledger.commit()
        except BaseException:
            ledger.rollback()
            raise
        tally.files += 1
        return "done"


def _skip_long(f, pos):
    """Offset just past the next newline at or after pos, or None at EOF."""
    f.seek(pos)
    while True:
        buf = f.read(CHUNK)
        if not buf:
            return None
        i = buf.find(b"\n")
        if i >= 0:
            return pos + i + 1
        pos += len(buf)


def _load_state(s):
    try:
        v = json.loads(s) if s else {}
    except ValueError:
        v = {}
    return v if isinstance(v, dict) else {}


def int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
