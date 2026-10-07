"""Quota freshness, time boundaries, prices, null-not-zero (plan §8.2)."""
import os
import unittest

from helpers import (HostCase, claude_line, codex_count, codex_meta, gemini_db, grok_report,
                     notes, rate_limits, table, tile, totals, ts)
from finops import report, util
from finops.prices import PriceError, Prices

H, D = 3600, 86400


class FreshnessRule(unittest.TestCase):
    def test_reset_in_the_past_is_reset(self):
        self.assertEqual(report.freshness(100.0, 10.0, 50.0, 5 * H, 5 * H), "reset")

    def test_old_observation_is_stale_even_with_future_reset(self):
        now = 10 * D
        self.assertEqual(report.freshness(now, now - 6 * H, now + H, 5 * H, 5 * H), "stale")
        self.assertEqual(report.freshness(now, now - 6 * H, now + D, 7 * D, 5 * H), "current")

    def test_no_reset_time_goes_stale_after_the_shortest_window(self):
        now = 10 * D
        self.assertEqual(report.freshness(now, now - 4 * H, None, 7 * D, 5 * H), "current")
        self.assertEqual(report.freshness(now, now - 6 * H, None, 7 * D, 5 * H), "stale")


class QuotaInTheDialog(HostCase):
    def claude_quota(self, windows, fp="fp-claude-1"):
        self.h.feed_json("quota.json", {"schema": "corral-light.module-feed/1",
                                        "accounts": {fp: {"lane": "claude",
                                                          "windows": windows}}})

    def obs(self, name, util_v, resets, age, status="allowed", unit="s", **kw):
        w = {"window": name, "rateLimitType": name, "utilization": util_v, "status": status,
             "resetsAt": resets * (1000 if unit == "ms" else 1), "resets_at_s": resets,
             "resets_at_unit": unit, "observed_at": self.h.now - age, "source": "notice"}
        w.update(kw)
        return w

    def test_claude_fraction_shown_as_percent_with_reset(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 0.16, self.h.now + 2 * H, 60)})
        snap, _ = self.h.run()
        t = tile(snap, "Claude 5 h")
        self.assertEqual((t["value"], t["kind"], t["level"]), ("16% used", "vendor", "ok"))
        self.assertIn("resets in 2 h", t["note"])

    def test_reset_since_last_report_has_no_status(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 0.95, self.h.now - 60, 2 * H,
                                                 status="rejected")})
        snap, _ = self.h.run()
        t = tile(snap, "Claude 5 h")
        self.assertEqual(t["value"], "reset since last report")
        self.assertNotIn("limit reached", t["note"])

    def test_stale_rejected_is_not_shown_as_current(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 1.0, self.h.now + H, 6 * H,
                                                 status="rejected")})
        snap, _ = self.h.run()
        t = tile(snap, "Claude 5 h")
        self.assertIn("stale", t["note"])
        self.assertNotIn("limit reached", t["note"])
        self.assertEqual(t["level"], "info")

    def test_current_rejected_is_bad(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 1.0, self.h.now + H, 60,
                                                 status="rejected")})
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Claude 5 h")["level"], "bad")

    def test_window_without_percent_never_says_zero(self):
        w = self.obs("seven_day_opus", None, self.h.now + D, 60)
        del w["utilization"]
        self.claude_quota({"seven_day_opus": w})
        snap, _ = self.h.run()
        t = tile(snap, "Claude weekly opus")
        self.assertEqual(t["value"], "no percent reported")

    def test_carried_field_is_as_old_as_its_own_time(self):
        w = self.obs("seven_day", 0.3, self.h.now + 6 * D, 60,
                     carried={"utilization": self.h.now - 8 * D})
        self.claude_quota({"seven_day": w})
        snap, _ = self.h.run()
        self.assertIn("stale", tile(snap, "Claude weekly")["note"])

    def test_codex_seconds_and_milliseconds_compare_correctly(self):
        future_ms = int((self.h.now + H) * 1000)
        self.h.write(os.path.join(self.h.codex, "a.jsonl"), [
            codex_meta("s1", "2026-10-07T19:59:00Z"),
            codex_count("2026-10-07T19:59:00Z", 1, 0, 1,
                        rate_limits(42.0, future_ms, 3.0, int(self.h.now + 3 * D)))])
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Codex 5 h")["value"], "42% used")
        self.assertIn("resets in 1 h", tile(snap, "Codex 5 h")["note"])

    def test_codex_past_reset_marks_the_window_reset(self):
        self.h.write(os.path.join(self.h.codex, "a.jsonl"), [
            codex_meta("s1", "2026-10-07T10:00:00Z"),
            codex_count("2026-10-07T10:00:00Z", 1, 0, 1,
                        rate_limits(80.0, int(self.h.now - H), 3.0, int(self.h.now + D)))])
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Codex 5 h")["value"], "reset since last report")

    def test_fingerprint_change_starts_the_new_account_empty(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 0.5, self.h.now + H, 60)},
                          fp="fp-old")
        self.h.logins(claude_fp="fp-new")
        led = self.h.ledger()
        led.close()
        snap, _ = self.h.run()
        accts = {a for a in _quota_accounts(self.h)}
        self.assertEqual(accts, {"claude:fp-old"})
        self.assertNotIn("claude:fp-new", accts)

    def test_level_thresholds(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 0.8, self.h.now + H, 60),
                           "seven_day": self.obs("seven_day", 0.93, self.h.now + D, 60)})
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Claude 5 h")["level"], "warn")
        self.assertEqual(tile(snap, "Claude weekly")["level"], "bad")


def _quota_accounts(h):
    from finops.sources.feed import Feed
    led = h.ledger()
    try:
        return [w["account"] for w in report.quota(led, Feed(h.feed), h.now)]
    finally:
        led.close()


class TimeBoundaries(HostCase):
    def test_last_night_of_the_month_in_the_configured_zone(self):
        # 2026-10-31 23:30 in Phoenix is 2026-11-01 06:30 UTC.
        h = self.h
        h.tz = "America/Phoenix"
        h.write(os.path.join(h.claude, "a.jsonl"), [
            claude_line("req_oct", "2026-11-01T06:30:00Z"),
            claude_line("req_nov", "2026-11-01T07:30:00Z")])
        now = ts("2026-11-01T08:00:00Z")                 # Nov 1, 01:00 local
        snap, _ = h.run(now=now)
        self.assertIn("November 2026", tile(snap, "API-equivalent")["label"])
        self.assertEqual(totals(h, since=util.month_start(now, util.zone("America/Phoenix")))
                         ["claude"][0], 115)

    def test_month_start_across_dst(self):
        ny = util.zone("America/New_York")
        # November starts in EDT (UTC-4); March starts in EST (UTC-5).
        self.assertEqual(util.month_start(ts("2026-11-15T12:00:00Z"), ny),
                         ts("2026-11-01T04:00:00Z"))
        self.assertEqual(util.month_start(ts("2026-03-20T12:00:00Z"), ny),
                         ts("2026-03-01T05:00:00Z"))

    def test_iso_parsing_keeps_nanoseconds_and_zones(self):
        self.assertEqual(util.iso_ns("1970-01-01T00:00:01.000000001"), 10 ** 9 + 1)
        self.assertEqual(util.iso_ns("1970-01-01T01:00:00+01:00"), 0)
        self.assertEqual(util.iso_ns(1500), 1500 * 10 ** 9)
        self.assertIsNone(util.iso_ns("yesterday"))


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.p = Prices.load()

    def test_every_row_has_an_https_source(self):
        for rows in self.p.rows.values():
            for r in rows:
                self.assertTrue(r["source"].startswith("https://"))

    def test_effective_date_reprices_only_later_days(self):
        before = self.p.cost("gemini-3.8-flash", ts("2026-12-31T23:59:59Z"), {"input": 10 ** 6})
        after = self.p.cost("gemini-3.8-flash", ts("2027-01-01T00:00:00Z"), {"input": 10 ** 6})
        self.assertEqual((before, after), (750000, 1500000))

    def test_long_context_row(self):
        short = self.p.cost("gpt-6-astra", 0, {"input": 10 ** 5}, input_tokens=10 ** 5)
        long_ = self.p.cost("gpt-6-astra", 0, {"input": 300000}, input_tokens=300000)
        self.assertEqual(short, 1000000)                # $10/MTok
        self.assertEqual(long_, 6000000)                # $20/MTok

    def test_missing_price_is_none_never_zero(self):
        self.assertIsNone(self.p.cost("no-such-model", 0, {"input": 5}))
        self.assertIsNone(self.p.cost(None, 0, {"input": 5}))

    def test_alias_is_a_lookup_not_a_rewrite(self):
        self.assertEqual(self.p.cost("gemini-3.8-flash-high", 0, {"output": 10 ** 6}),
                         3750000)

    def test_cache_dimension_without_a_price_is_charged_at_input(self):
        # gpt rows have no cache_write: it falls back to input, never free.
        self.assertEqual(self.p.cost("gpt-6-astra", 0, {"cache_write_5m": 10 ** 6},
                                     input_tokens=0), 10 * 10 ** 6)

    def test_a_row_without_a_source_is_refused(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[[price]]\nmodel = "m"\ninput = 1\noutput = 1\n')
        try:
            with self.assertRaises(PriceError):
                Prices.load(f.name)
        finally:
            os.unlink(f.name)


class NullNotZero(HostCase):
    def test_nothing_in_view_says_unreported(self):
        snap, _ = self.h.run(feed=False)
        self.assertEqual(tile(snap, "Committed")["value"], "unreported")
        self.assertEqual(tile(snap, "Quota")["value"], "unreported")
        self.assertEqual(tile(snap, "API-equivalent")["value"], "unreported")
        self.assertTrue(any("feed is not in view" in n for n in notes(snap)))

    def test_each_source_broken_in_turn_leaves_the_others(self):
        h = self.h
        at = ts("2026-10-05T00:00:00Z")
        h.write(os.path.join(h.claude, "a.jsonl"), [claude_line("req_1", "2026-10-05T00:00:00Z")])
        h.write(os.path.join(h.codex, "a.jsonl"), [codex_meta("s1", "2026-10-05T00:00:00Z"),
                                                   codex_count("2026-10-05T00:00:01Z", 10, 0, 1)])
        gemini_db(os.path.join(h.gemini, "44444444-dddd.db"), "44444444-dddd",
                  [(1, at, 10, 0, 0, 1, "gemini-3.8-flash")]).close()
        h.grok(grok_report("aaaaaaaa-1111", [(1, "2026-10-05T00:00:00", 1, 1, 10 ** 10)]))
        snap, _ = h.run()
        full = {r[1]: r[4] for r in table(snap, "By account")["rows"]}
        self.assertEqual(set(full), {"Claude", "Codex", "Grok", "Gemini"})
        # Break Claude's store: the others are unchanged and Claude keeps
        # what it had (facts are not erased by a broken file).
        renamed = ('{"type": "assistant", "timestamp": "2026-10-06T00:00:00Z", "requestId": '
                   '"r%d", "message": {"model": "m", "usage": {"inTokens": 1}}}\n')
        with open(os.path.join(h.claude, "a.jsonl"), "a") as f:
            f.write("".join(renamed % i for i in range(30)))
        snap2, res = h.run()
        again = {r[1]: r[4] for r in table(snap2, "By account")["rows"]}
        self.assertEqual(again, full)
        self.assertIn("claude", res.frozen)
        rows = {r[0]: r[1] for r in table(snap2, "Sources")["rows"]}
        self.assertEqual(rows["Claude"], "format changed")
        self.assertEqual(rows["Codex"], "reported")


if __name__ == "__main__":
    unittest.main()
