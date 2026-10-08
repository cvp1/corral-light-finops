"""Rail notices (plan §4.7, §8.2 "Notices"): near-limit quota windows and
frozen sources, at the same levels as their tiles."""
import os
import time
import unittest

from helpers import SENTINEL, HostCase, claude_line, tile
from light_contract import validate_snapshot
from finops import scan, view
from finops.config import Config

H, D = 3600, 86400


class Notices(HostCase):
    def claude_quota(self, windows, fp="fp-claude-1"):
        self.h.feed_json("quota.json", {"schema": "corral-light.module-feed/1",
                                        "accounts": {fp: {"lane": "claude",
                                                          "windows": windows}}})

    def obs(self, name, util_v, resets, age, status="allowed", **kw):
        w = {"window": name, "rateLimitType": name, "utilization": util_v, "status": status,
             "resetsAt": resets, "resets_at_s": resets, "resets_at_unit": "s",
             "observed_at": self.h.now - age, "source": "notice"}
        w.update(kw)
        return w

    def notices(self, snap):
        return {n["id"]: n for n in snap.get("notices", [])}

    def test_thresholds_match_the_tiles(self):
        for pct, level in ((0.74, None), (0.75, "warn"), (0.89, "warn"), (0.90, "bad")):
            with self.subTest(pct=pct):
                self.claude_quota({"five_hour": self.obs("five_hour", pct, self.h.now + H, 60)})
                snap, _ = self.h.run()
                got = self.notices(snap).get("quota.claude-fp-claude-1.five_hour")
                self.assertEqual(got and got["level"], level)
                if got:
                    self.assertEqual(got["level"], tile(snap, "Claude 5 h")["level"])
                    self.assertEqual(got["title"], f"Claude 5 h {round(pct * 100)}% used")

    def test_vendor_status_raises_the_level(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 0.5, self.h.now + H, 60,
                                                 status="allowed_warning"),
                           "seven_day": self.obs("seven_day", 0.2, self.h.now + D, 60,
                                                 status="rejected")})
        n = self.notices(self.h.run()[0])
        self.assertEqual(n["quota.claude-fp-claude-1.five_hour"]["level"], "warn")
        self.assertEqual(n["quota.claude-fp-claude-1.seven_day"]["level"], "bad")

    def test_stale_and_reset_windows_raise_nothing(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 1.0, self.h.now + H, 6 * H,
                                                 status="rejected"),
                           "seven_day": self.obs("seven_day", 0.99, self.h.now - 60, H,
                                                 status="rejected")})
        self.assertEqual(self.h.run()[0]["notices"], [])

    def test_expiry_is_the_reset_or_observation_plus_length(self):
        w = self.obs("seven_day", 0.95, self.h.now + D, 60)
        self.claude_quota({"seven_day": w})
        n = self.notices(self.h.run()[0])["quota.claude-fp-claude-1.seven_day"]
        self.assertEqual(n["expires_at"], "2026-10-08T20:00:00Z")
        w = self.obs("seven_day", 0.95, None, 60)
        for k in ("resetsAt", "resets_at_s"):
            w.pop(k)
        self.claude_quota({"seven_day": w})
        n = self.notices(self.h.run()[0])["quota.claude-fp-claude-1.seven_day"]
        self.assertEqual(n["expires_at"], "2026-10-14T19:59:00Z")

    def test_ids_are_valid_and_stable(self):
        self.assertEqual(view.notice_id("quota", "claude:AB/c", "x"), "quota.claude-ab-c.x")
        long_id = view.notice_id("quota", "codex:" + "z" * 80, "primary")
        self.assertEqual(len(long_id), 64)
        self.assertEqual(long_id, view.notice_id("quota", "codex:" + "z" * 80, "primary"))
        self.assertNotEqual(long_id, view.notice_id("quota", "codex:" + "z" * 80, "secondary"))
        self.claude_quota({"five_hour": self.obs("five_hour", 0.95, self.h.now + H, 60)},
                          fp="Fp:" + "Q" * 90)
        snap, _ = self.h.run()
        out, err = validate_snapshot(__import__("json").dumps(snap), notices=True)
        self.assertIsNone(err)
        self.assertEqual(len(out["notices"]), 1)
        self.assertEqual(out["notices"][0]["id"], snap["notices"][0]["id"])

    def test_a_frozen_source_warns_until_a_module_update(self):
        led = self.h.ledger()
        led.set_source_state("gemini", "format_changed", "only 1 of 30 parsed", time.time(),
                             scan.VERSION)
        led.close()
        n = self.notices(self.h.run()[0])
        self.assertEqual(n["source.gemini.frozen"]["level"], "warn")
        led = self.h.ledger()
        led.set_source_state("gemini", "format_changed", "x", time.time(), "0.0.0-old")
        led.close()
        self.assertNotIn("source.gemini.frozen", self.notices(self.h.run()[0]))

    def test_turned_off_in_config(self):
        self.claude_quota({"five_hour": self.obs("five_hour", 0.95, self.h.now + H, 60)})
        with open(os.path.join(self.h.cfgdir, "config.toml"), "w") as f:
            f.write('timezone = "UTC"\nnotices = "off"\n')
        self.assertNotIn("notices", self.h.run()[0])
        cfg = Config.load(os.path.join(self.h.cfgdir, "config.toml"))
        self.assertIn('notices = "off"', cfg.dumps())

    def test_no_prompt_text_reaches_a_notice(self):
        self.h.write(os.path.join(self.h.claude, "c.jsonl"),
                     [claude_line("req_1", "2026-10-07T19:00:00Z")])
        self.claude_quota({"five_hour": self.obs("five_hour", 0.95, self.h.now + H, 60)})
        snap, _ = self.h.run()
        self.assertTrue(snap["notices"])
        self.assertNotIn(SENTINEL, __import__("json").dumps(snap["notices"]))


if __name__ == "__main__":
    unittest.main()
