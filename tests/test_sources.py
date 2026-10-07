"""Readers: Claude, Codex, Grok, Gemini, and the per-file cursors (plan §8.2)."""
import os
import unittest

from helpers import (HostCase, claude_line, codex_count, codex_ctx, codex_meta, codex_message,
                     codex_record, gemini_db, grok_report, rate_limits, totals, ts, user_line)
from finops import report
from finops.prices import Prices
from finops.sources import codex as codex_src

T0 = "2026-10-07T10:00:00.000Z"


def claude_rows(h):
    led = h.ledger()
    try:
        return led.q("SELECT request_id, output, path FROM claude_req ORDER BY request_id")
    finally:
        led.close()


class ClaudeTests(HostCase):
    def test_one_request_in_two_trees_counts_once(self):
        line = claude_line("req_1", T0, out=7)
        self.h.write(os.path.join(self.h.claude, "a.jsonl"), [line])
        self.h.write(os.path.join(self.h.panes, "a.jsonl"), [line])
        self.h.run()
        self.assertEqual(len(claude_rows(self.h)), 1)
        self.assertEqual(totals(self.h)["claude"][0], 10 + 7 + 100)

    def test_streamed_revision_keeps_the_largest_output(self):
        p = os.path.join(self.h.claude, "a.jsonl")
        self.h.write(p, [claude_line("req_1", T0, out=1), claude_line("req_1", T0, out=40),
                         claude_line("req_1", T0, out=12)])
        self.h.run()
        self.assertEqual(claude_rows(self.h)[0][1], 40)

    def test_revision_order_does_not_matter(self):
        a = [claude_line("req_1", T0, out=n) for n in (3, 9, 1)]
        self.h.write(os.path.join(self.h.claude, "a.jsonl"), a[:1])
        self.h.write(os.path.join(self.h.panes, "b.jsonl"), a[1:])
        self.h.run()
        self.assertEqual(claude_rows(self.h)[0][1], 9)

    def test_missing_request_id_is_counted_apart_and_said(self):
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [claude_line(None, T0, out=5), claude_line(None, T0, out=5)])
        snap, _ = self.h.run()
        self.assertEqual(totals(self.h)["claude"][0], 2 * 115)
        self.assertTrue(any("no request id" in n for n in _notes(snap)))

    def test_synthetic_is_skipped(self):
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [claude_line("req_s", T0, model="<synthetic>")])
        self.h.run()
        self.assertEqual(claude_rows(self.h), [])

    def test_unknown_model_is_unpriced_not_zero(self):
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [claude_line("req_1", T0, model="claude-future-9")])
        snap, _ = self.h.run()
        tok, micros, _v, unpriced = totals(self.h)["claude"]
        self.assertEqual((micros, unpriced), (0, tok))
        self.assertTrue(any("claude-future-9" in n for n in _notes(snap)))
        acct = [r for r in snap_table(snap, "By account")["rows"] if r[1] == "Claude"][0]
        self.assertEqual(acct[5], "unpriced")

    def test_user_lines_and_other_types_are_not_candidates(self):
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [user_line(T0, text='"assistant" "usage"'), claude_line("req_1", T0)])
        _snap, res = self.h.run()
        self.assertEqual(res.tallies["claude"].bad, 0)

    def test_claude_list_price_matches_the_published_rate(self):
        # 1M input + 1M output on Opus 5.5 = $4 + $20.
        self.h.write(os.path.join(self.h.claude, "a.jsonl"),
                     [claude_line("req_1", T0, inp=10 ** 6, out=10 ** 6, cr=0)])
        self.h.run()
        self.assertEqual(totals(self.h)["claude"][1], 24 * 10 ** 6)


class CodexTests(HostCase):
    def rollout(self, name, lines):
        return self.h.write(os.path.join(self.h.codex, name), lines)

    def test_records_keyed_by_response_id_repeats_ignored(self):
        lines = [codex_meta("s1", T0), codex_ctx(T0),
                 codex_record("r1", T0, 1000, 400, 50), codex_record("r1", T0, 1000, 400, 50),
                 codex_record("r2", "2026-10-07T10:01:00Z", 2000, 1500, 20),
                 codex_count("2026-10-07T10:01:00Z", 3000, 1900, 70)]
        self.rollout("a.jsonl", lines)
        self.h.run()
        led = self.h.ledger()
        try:
            self.assertEqual(led.one("SELECT COUNT(*) FROM codex_resp"), 2)
        finally:
            led.close()
        # The cumulative count is ignored for a session with records.
        self.assertEqual(totals(self.h)["codex"][0], 1050 + 2020)

    def test_cumulative_to_delta_reset_duplicates_and_order(self):
        rows = [  # (ts, total, account, model, i, c, o, r)
            (2.0, 300, "a", "m", 250, 0, 50, 0),
            (1.0, 100, "a", "m", 90, 0, 10, 0),
            (2.0, 300, "a", "m", 250, 0, 50, 0),        # duplicate
            (3.0, 300, "a", "m", 250, 0, 50, 0),        # equal total later: ignored
            (4.0, 40, "a", "m", 30, 0, 10, 0),          # reset
            (5.0, 90, "a", "m", 70, 0, 20, 0)]
        d = codex_src.deltas(rows)
        self.assertEqual([(x[0], x[3], x[5]) for x in d],
                         [(1.0, 90, 10), (2.0, 160, 40), (4.0, 30, 10), (5.0, 40, 10)])
        self.assertTrue(all(min(x[3:]) >= 0 for x in d))
        import random
        shuffled = list(rows)
        random.Random(4).shuffle(shuffled)
        self.assertEqual(codex_src.deltas(shuffled), d)

    def test_older_rollout_without_records_uses_deltas(self):
        self.rollout("a.jsonl", [codex_meta("s1", T0), codex_ctx(T0),
                                 codex_count("2026-10-07T10:00:01Z", 100, 0, 10),
                                 codex_count("2026-10-07T10:00:02Z", 300, 0, 30)])
        self.h.run()
        self.assertEqual(totals(self.h)["codex"][0], 330)

    def test_shuffled_file_order_gives_the_same_totals(self):
        a = [codex_meta("s1", T0), codex_ctx(T0), codex_count("2026-10-07T10:00:01Z", 100, 0, 10)]
        b = [codex_meta("s1", T0), codex_ctx(T0), codex_count("2026-10-07T10:00:02Z", 300, 0, 30)]
        self.rollout("z.jsonl", a)
        self.rollout("a.jsonl", b)
        self.h.run()
        self.assertEqual(totals(self.h)["codex"][0], 330)

    def test_session_first_seen_mid_history_is_marked(self):
        self.rollout("a.jsonl", [codex_count("2026-10-07T10:00:02Z", 300, 0, 30)])
        self.h.run()
        led = self.h.ledger()
        try:
            self.assertEqual(led.one("SELECT mid_history FROM codex_session"), 1)
        finally:
            led.close()

    def test_rate_limits_absent_is_unreported(self):
        self.rollout("a.jsonl", [codex_meta("s1", T0), codex_count(T0, 1, 0, 1)])
        snap, _ = self.h.run()
        self.assertFalse(any(it["label"].startswith("Codex") for b in snap["view"]
                             if b["type"] == "tiles" for it in b["items"]))

    def test_newest_quota_by_event_time_not_file_order(self):
        reset = self.h.now + 3600
        self.rollout("a.jsonl", [codex_meta("s1", T0),
                                 codex_count("2026-10-07T19:00:00Z", 1, 0, 1,
                                             rate_limits(61.0, reset, 7.0, reset + 86400))])
        self.rollout("b.jsonl", [codex_meta("s2", T0),
                                 codex_count("2026-10-07T18:00:00Z", 1, 0, 1,
                                             rate_limits(10.0, reset, 1.0, reset + 86400))])
        snap, _ = self.h.run()
        t = _tile(snap, "Codex 5 h")
        self.assertEqual(t["value"], "61% used")

    def test_account_is_hashed_never_raw(self):
        self.rollout("a.jsonl", [codex_meta("s1", T0, account="raw-account-id-123"),
                                 codex_count(T0, 1, 0, 1)])
        self.h.run()
        with open(os.path.join(self.h.data, "ledger.db"), "rb") as f:
            blob = f.read()
        self.assertNotIn(b"raw-account-id-123", blob)

    def test_two_homes_same_account_one_proposal_different_two(self):
        other = self.h.mk("codex2/sessions")
        self.rollout("a.jsonl", [codex_meta("s1", T0, "acct-1"), codex_count(T0, 1, 0, 1)])
        self.h.write(os.path.join(other, "b.jsonl"),
                     [codex_meta("s2", T0, "acct-1"), codex_count(T0, 1, 0, 1)])
        env = self.h.env()
        env.reads["codex-sessions"].append(other)
        _accounts = lambda: [a for a in _accts(self.h, env) if a["vendor"] == "codex"]  # noqa
        self.assertEqual(len(_accounts()), 1)
        self.h.write(os.path.join(other, "c.jsonl"),
                     [codex_meta("s3", T0, "acct-2"), codex_count(T0, 1, 0, 1)])
        self.assertEqual(len(_accounts()), 2)


class GrokTests(HostCase):
    def test_turns_dated_by_turn_and_ticks_stay_integers(self):
        self.h.grok(grok_report("aaaaaaaa-1111", [
            (1, "2026-09-30T23:59:59.5", 100, 10, 3 * 10 ** 10),     # last month
            (2, "2026-10-01T00:00:01.25", 100, 10, 12345678901)]))
        self.h.run()
        led = self.h.ledger()
        try:
            ticks = led.q("SELECT ticks FROM grok_turn ORDER BY turn")
        finally:
            led.close()
        self.assertTrue(all(isinstance(t[0], int) for t in ticks))
        month = totals(self.h, since=ts("2026-10-01T00:00:00Z"))["grok"]
        self.assertEqual(month[2], 12345678901 // 10 ** 4)

    def test_identical_turns_are_both_counted(self):
        self.h.grok(grok_report("aaaaaaaa-1111", [(1, "2026-10-02T00:00:00", 5, 5, 10 ** 9),
                                                  (2, "2026-10-02T00:00:00", 5, 5, 10 ** 9)]))
        self.h.run()
        self.assertEqual(totals(self.h)["grok"][2], 2 * 10 ** 5)

    def test_fork_inherited_turns_not_counted_resume_once(self):
        parent = [(1, "2026-10-02T00:00:00.000000001", 5, 5, 10 ** 10),
                  (2, "2026-10-02T01:00:00", 5, 5, 10 ** 10)]
        self.h.grok(grok_report("aaaaaaaa-1111", parent))
        self.h.grok(grok_report("bbbbbbbb-2222", parent + [(3, "2026-10-03T00:00:00", 5, 5,
                                                            10 ** 10)],
                                parent="aaaaaaaa-1111", forked_at="2026-10-02T01:00:00"))
        self.h.run()
        self.assertEqual(totals(self.h)["grok"][2], 3 * 10 ** 6)
        # A resume rewrites the same session's report with one more turn.
        self.h.grok(grok_report("aaaaaaaa-1111", parent + [(3, "2026-10-04T00:00:00", 5, 5,
                                                            10 ** 10)]))
        self.h.run()
        self.assertEqual(totals(self.h)["grok"][2], 4 * 10 ** 6)

    def test_vendor_cost_and_list_estimate_stay_apart(self):
        self.h.grok(grok_report("aaaaaaaa-1111", [(1, "2026-10-02T00:00:00", 10 ** 6, 0,
                                                   7 * 10 ** 10)]))
        snap, _ = self.h.run()
        self.assertEqual(_tile(snap, "Grok,")["value"], "$7.00")
        self.assertEqual(_tile(snap, "Grok,")["kind"], "vendor")
        # grok-4.7 list: 1M input at $2.00; reported beside, not instead.
        self.assertEqual(totals(self.h)["grok"][1], 2 * 10 ** 6)
        row = [r for r in snap_table(snap, "By account")["rows"] if r[1] == "Grok"][0]
        self.assertEqual((row[5], row[6]), ("$2.00", "$7.00"))

    def test_stale_report_keeps_turns_and_is_said(self):
        self.h.grok(grok_report("aaaaaaaa-1111", [(1, "2026-10-02T00:00:00", 1, 1, 10 ** 10)],
                                stale=True))
        snap, _ = self.h.run()
        self.assertEqual(totals(self.h)["grok"][2], 10 ** 6)
        src = [r for r in snap_table(snap, "Sources")["rows"] if r[0] == "Grok"][0]
        self.assertIn("1 stale", src[2])


class GeminiTests(HostCase):
    def test_calls_read_with_model_and_alias_pricing(self):
        at = ts("2026-10-05T00:00:00Z")
        gemini_db(os.path.join(self.h.gemini, "11111111-aaaa.db"), "11111111-aaaa",
                  [(1, at, 10 ** 6, 0, 0, 0, "gemini-3.8-flash"),
                   (3, at, 0, 0, 0, 10 ** 6, None)]).close()
        self.h.run()
        tok, micros, _v, unpriced = totals(self.h)["gemini"]
        # $0.75 input via the gen model; the alias-only step prices as base.
        self.assertEqual((tok, unpriced), (2 * 10 ** 6, 0))
        self.assertEqual(micros, 750000 + 3750000)

    def test_wal_contents_are_read_through_a_copy(self):
        at = ts("2026-10-05T00:00:00Z")
        p = os.path.join(self.h.gemini, "22222222-bbbb.db")
        con = gemini_db(p, "22222222-bbbb", [(1, at, 1000, 0, 0, 10, "gemini-3.8-flash")],
                        wal=True)
        try:
            self.assertTrue(os.path.getsize(p + "-wal") > 0)
            os.chmod(self.h.gemini, 0o555)       # the store is read-only to us
            self.h.run()
        finally:
            os.chmod(self.h.gemini, 0o755)
            con.close()
        self.assertEqual(totals(self.h)["gemini"][0], 1010)
        self.assertEqual(os.listdir(os.path.join(self.h.data, "tmp", "gemini")), [])

    def test_canary_failure_freezes_the_source(self):
        at = ts("2026-10-05T00:00:00Z")
        p = os.path.join(self.h.gemini, "33333333-cccc.db")
        con = gemini_db(p, "33333333-cccc", [(i, at, 1, 0, 0, 1, "gemini-3.8-flash")
                                             for i in range(25)])
        # Break the canary: output total no longer equals thinking + visible.
        from helpers import pb_bytes, pb_int
        bad = pb_bytes(1, pb_int(1, int(at))) + pb_bytes(9, pb_int(2, 1) + pb_int(3, 99) +
                                                         pb_int(10, 1))
        con.execute("UPDATE steps SET metadata=?", (bad,))
        con.commit()
        con.close()
        snap, res = self.h.run()
        self.assertIn("gemini", res.frozen)
        src = [r for r in snap_table(snap, "Sources")["rows"] if r[0] == "Gemini"][0]
        self.assertEqual(src[1], "format changed")


class CursorTests(HostCase):
    def path(self):
        return os.path.join(self.h.claude, "c.jsonl")

    def test_append_then_partial_line_and_split_utf8(self):
        p = self.path()
        self.h.write(p, [claude_line("req_1", T0)])
        self.h.run()
        line = claude_line("req_2", T0, text="café ☃").encode("utf-8")
        cut = line.index("☃".encode()) + 1          # inside the snowman
        with open(p, "ab") as f:
            f.write(line[:cut])
        self.h.run()
        self.assertEqual(len(claude_rows(self.h)), 1)
        with open(p, "ab") as f:
            f.write(line[cut:] + b"\n")
        _snap, res = self.h.run()
        self.assertEqual(len(claude_rows(self.h)), 2)
        self.assertEqual(res.tallies["claude"].bad, 0)

    def test_truncate_and_rotate_reread_from_start(self):
        p = self.path()
        self.h.write(p, [claude_line(None, T0), claude_line(None, T0)])
        self.h.run()
        self.h.write(p, [claude_line(None, T0)])                    # truncated
        self.h.run()
        self.assertEqual(totals(self.h)["claude"][0], 115)
        os.unlink(p)
        self.h.write(p, [claude_line(None, T0), claude_line(None, T0), claude_line(None, T0)])
        self.h.run()                                                # new inode
        self.assertEqual(totals(self.h)["claude"][0], 3 * 115)

    def test_deleted_file_keeps_its_facts(self):
        p = self.path()
        self.h.write(p, [claude_line("req_1", T0)])
        self.h.run()
        os.unlink(p)
        self.h.run()
        self.assertEqual(len(claude_rows(self.h)), 1)

    def test_long_line_is_skipped_and_counted(self):
        from finops.sources import lines
        p = self.path()
        big = claude_line("req_big", T0, text="x" * (lines.LONG_LINE + 10))
        self.h.write(p, [big, claude_line("req_2", T0)])
        _snap, res = self.h.run()
        self.assertEqual([r[0] for r in claude_rows(self.h)], ["req_2"])
        self.assertEqual(res.tallies["claude"].long, 1)

    def test_line_longer_than_a_chunk(self):
        from finops.sources import lines
        old = lines.CHUNK
        lines.CHUNK = 4096
        try:
            p = self.path()
            self.h.write(p, [claude_line("req_big", T0, text="y" * 10000),
                             claude_line("req_2", T0)])
            self.h.run()
        finally:
            lines.CHUNK = old
        self.assertEqual([r[0] for r in claude_rows(self.h)], ["req_2"])


def _notes(snap):
    return [b["text"] for b in snap["view"] if b["type"] == "note"]


def _tile(snap, start):
    for b in snap["view"]:
        if b["type"] == "tiles":
            for it in b["items"]:
                if it["label"].startswith(start):
                    return it
    return None


def snap_table(snap, start):
    return next(b for b in snap["view"] if b["type"] == "table" and b["title"].startswith(start))


def _accts(h, env):
    from finops import scan
    from finops.config import Config, load_plans
    led, feed, _res = scan.run(env, 25)
    try:
        return report.accounts(led, feed, Config.load(env.config), load_plans())
    finally:
        led.close()


if __name__ == "__main__":
    unittest.main()
