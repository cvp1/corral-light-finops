"""Replay invariance, budgeted backfill, ledger schema, drift (plan §6.5, §8.2)."""
import os
import random
import sqlite3
import time
import unittest

from helpers import (HostCase, claude_line, codex_count, codex_meta, codex_record, notes,
                     tile, totals, ts)
from finops import ledger as ledger_mod
from finops.sources import lines


def claude_history(h, n_files, per_file, start="2026-10-01", seed=1, dup_every=7):
    rnd = random.Random(seed)
    paths = []
    for fi in range(n_files):
        recs = []
        for i in range(per_file):
            rid = f"req_{fi}_{i}"
            for out in sorted({1, rnd.randint(2, 50), 60}):      # streamed revisions
                recs.append(claude_line(rid, f"{start}T{(i % 24):02d}:00:00Z", out=out))
            if i % dup_every == 0:
                recs.append(claude_line(f"req_{(fi + 1) % n_files}_{i}",
                                        f"{start}T{(i % 24):02d}:00:00Z", out=60))
        rnd.shuffle(recs)
        root = h.claude if fi % 2 else h.panes
        paths.append(h.write(os.path.join(root, f"f{fi}.jsonl"), recs))
    return paths


class ReplayInvariance(HostCase):
    def reference(self, build):
        other = type(self.h)()
        try:
            build(other)
            other.run()
            return totals(other)
        finally:
            other.cleanup()

    def test_shuffled_duplicated_and_crashed_reads_give_identical_totals(self):
        def build(h):
            claude_history(h, 6, 40)
            h.write(os.path.join(h.codex, "a.jsonl"),
                    [codex_meta("s1", "2026-10-02T00:00:00Z")] +
                    [codex_record(f"r{i}", "2026-10-02T00:00:00Z", 100 + i, 10, 5)
                     for i in range(50)] * 2)
        want = self.reference(build)
        build(self.h)
        old_chunk, real_commit = lines.CHUNK, ledger_mod.Ledger.commit
        lines.CHUNK = 2048
        try:
            for crash_at in (1, 3, 8, 15, 40):
                calls = {"n": 0}

                def commit(self_, _real=real_commit, _c=calls, _k=crash_at):
                    _real(self_)
                    _c["n"] += 1
                    if _c["n"] == _k:
                        raise KeyboardInterrupt("crash after a batch")
                ledger_mod.Ledger.commit = commit
                try:
                    self.h.run()
                except KeyboardInterrupt:
                    pass
                finally:
                    ledger_mod.Ledger.commit = real_commit
            self.h.run()
        finally:
            lines.CHUNK = old_chunk
            ledger_mod.Ledger.commit = real_commit
        self.assertEqual(totals(self.h), want)

    def test_reading_the_same_history_twice_changes_nothing(self):
        claude_history(self.h, 3, 20)
        self.h.run()
        first = totals(self.h)
        # Copies of every file under another home.
        for name in os.listdir(self.h.panes):
            with open(os.path.join(self.h.panes, name), "rb") as f:
                data = f.read()
            with open(os.path.join(self.h.claude, "copy-" + name), "wb") as f:
                f.write(data)
        self.h.run()
        self.assertEqual(totals(self.h), first)


class Backfill(HostCase):
    def test_budgeted_backfill_fills_current_month_first_with_progress(self):
        old = claude_history(self.h, 8, 300, start="2026-08-03", seed=2)
        for p in old:
            os.utime(p, (time.time() - 60 * 86400,) * 2)
        new = self.h.write(os.path.join(self.h.claude, "now.jsonl"),
                           [claude_line(f"req_new_{i}", "2026-10-06T00:00:00Z")
                            for i in range(50)])
        old_chunk = lines.CHUNK
        lines.CHUNK = 64 << 10
        try:
            snap, res = self.h.run(budget=0.001)
            self.assertFalse(res.complete)
            self.assertIsNotNone(snap.get("progress"))
            self.assertEqual(snap["progress"]["phase"], "backfill")
            october = totals(self.h, since=ts("2026-10-01T00:00:00Z"))
            self.assertEqual(october["claude"][0], 50 * 115)       # current month is in
            pcts = [snap["progress"]["done_pct"]]
            for _ in range(200):
                snap, res = self.h.run(budget=0.001)
                if res.complete:
                    break
                pcts.append(snap["progress"]["done_pct"])
            self.assertTrue(res.complete)
            self.assertIsNone(snap.get("progress"))
            self.assertEqual(pcts, sorted(pcts))
        finally:
            lines.CHUNK = old_chunk
        self.assertTrue(os.path.exists(new))

    def test_incremental_run_with_one_mb_of_new_lines_is_fast(self):
        p = self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                         [claude_line(f"r{i}", "2026-10-06T00:00:00Z") for i in range(2000)])
        self.h.run()
        line = claude_line("x", "2026-10-06T00:00:00Z")
        n = (1 << 20) // (len(line) + 1)
        self.h.write(p, [claude_line(f"n{i}", "2026-10-06T00:00:00Z") for i in range(n)],
                     mode="a")
        t0 = time.monotonic()
        self.h.run()
        self.assertLess(time.monotonic() - t0, 2.0)


class Schema(HostCase):
    def test_a_newer_ledger_is_set_aside_never_written(self):
        led = self.h.ledger()
        led.db.execute("UPDATE meta SET value='99' WHERE key='schema'")
        led.close()
        snap, _ = self.h.run()
        aside = [n for n in os.listdir(self.h.data) if n.startswith("ledger.db.v99")]
        self.assertTrue(aside)
        con = sqlite3.connect(os.path.join(self.h.data, aside[0]))
        self.assertEqual(con.execute("SELECT value FROM meta").fetchone()[0], "99")
        con.close()
        self.assertTrue(any("newer FinOps" in n for n in notes(snap)))

    def test_an_interrupted_migration_leaves_the_old_ledger_usable(self):
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [claude_line("req_1", "2026-10-06T00:00:00Z")])
        self.h.run()
        led = self.h.ledger()
        led.db.execute("UPDATE meta SET value='0' WHERE key='schema'")
        led.close()
        ledger_mod.MIGRATIONS[0] = "CREATE TABLE extra (x); SELECT no_such_function()"
        try:
            with self.assertRaises(sqlite3.Error):
                self.h.ledger()
        finally:
            del ledger_mod.MIGRATIONS[0]
        con = sqlite3.connect(os.path.join(self.h.data, "ledger.db"))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("extra", tables)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM claude_req").fetchone()[0], 1)
        con.close()
        self.assertTrue(os.path.exists(os.path.join(self.h.data, "ledger.db.v0.bak")))

    def test_a_migration_that_works_moves_the_version(self):
        led = self.h.ledger()
        led.db.execute("UPDATE meta SET value='0' WHERE key='schema'")
        led.close()
        ledger_mod.MIGRATIONS[0] = "CREATE TABLE IF NOT EXISTS extra (x)"
        try:
            led = self.h.ledger()
            self.assertEqual(led.one("SELECT value FROM meta WHERE key='schema'"), "1")
            led.close()
        finally:
            del ledger_mod.MIGRATIONS[0]

    def test_an_unreadable_ledger_is_rebuilt(self):
        with open(os.path.join(self.h.data, "ledger.db"), "wb") as f:
            f.write(b"not a database at all" * 100)
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [claude_line("req_1", "2026-10-06T00:00:00Z")])
        self.h.run()
        self.assertEqual(totals(self.h)["claude"][0], 115)


class Drift(HostCase):
    def break_claude(self):
        bad = ('{"type": "assistant", "timestamp": "2026-10-06T00:00:00Z", "requestId": '
               '"r%d", "message": {"model": "m", "usage": {"inTokens": 1}}}')
        self.h.write(os.path.join(self.h.claude, "a.jsonl"), [bad % i for i in range(30)])

    def test_frozen_until_a_new_version(self):
        self.break_claude()
        _snap, res = self.h.run()
        self.assertIn("claude", res.frozen)
        self.h.write(os.path.join(self.h.claude, "b.jsonl"),
                     [claude_line("req_ok", "2026-10-06T00:00:00Z")])
        self.h.run()
        self.assertNotIn("claude", totals(self.h))               # frozen: not read
        import finops.scan as scan_mod
        old = scan_mod.VERSION
        scan_mod.VERSION = "9.9.9"
        try:
            self.h.run()
        finally:
            scan_mod.VERSION = old
        self.assertEqual(totals(self.h)["claude"][0], 115)

    def test_a_few_bad_lines_do_not_freeze(self):
        bad = '{"type": "assistant", "message": {"usage": {"x": 1}}}'
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [bad] * 2 + [claude_line(f"r{i}", "2026-10-06T00:00:00Z")
                                  for i in range(40)])
        _snap, res = self.h.run()
        self.assertEqual(res.frozen, [])


if __name__ == "__main__":
    unittest.main()
