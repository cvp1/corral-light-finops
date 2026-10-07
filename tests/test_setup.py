"""Automatic setup: proposals, acceptance, typed prices (plan §6.3, §8.2)."""
import io
import os
import unittest

from helpers import HostCase, claude_line, codex_count, codex_meta, rate_limits, table, tile
from finops import main, tomlmini
from finops.config import Config, load_plans, match_plan

T0 = "2026-10-05T00:00:00Z"


class SetupTests(HostCase):
    def seed(self, claude_tier="default_claude_max_5x", codex_plan="plus"):
        self.h.logins(tier=claude_tier)
        self.h.write(os.path.join(self.h.claude, "a.jsonl"), [claude_line("req_1", T0)])
        self.h.write(os.path.join(self.h.codex, "a.jsonl"), [
            codex_meta("s1", T0), codex_count(T0, 1, 0, 1, rate_limits(plan=codex_plan))])

    def setup(self, *answers, yes=False):
        it = iter(answers)
        out = io.StringIO()
        rc = main.setup(self.h.env(), yes=yes, out=out, ask=lambda _p: next(it, ""))
        return rc, out.getvalue()

    def cfg(self):
        return Config.load(self.h.env().config)

    def test_first_run_proposes_with_no_config(self):
        self.seed()
        snap, _ = self.h.run()
        rows = table(snap, "By account")["rows"]
        self.assertTrue(all(r[0].endswith("(proposed)") for r in rows))
        t = tile(snap, "Committed")
        self.assertEqual(t["kind"], "list")
        self.assertEqual(t["value"], "$120 / mo")
        self.assertIn("unconfirmed", t["note"])

    def test_setup_yes_accepts_accounts_and_declares_no_price(self):
        self.seed()
        rc, _ = self.setup(yes=True)
        self.assertEqual(rc, 0)
        cfg = self.cfg()
        self.assertEqual({a["vendor"] for a in cfg.accounts}, {"claude", "codex", "grok"})
        self.assertFalse(any("usd_cents_month" in a for a in cfg.accounts))
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Committed")["kind"], "list")
        self.assertFalse(any(r[0].endswith("(proposed)")
                             for r in table(snap, "By account")["rows"]))

    def test_typed_amount_becomes_declared_with_provenance(self):
        self.seed()
        # claude: accept, $100 ; codex: accept, skip price ; grok: accept, 30
        rc, out = self.setup("y", "100", "y", "", "y", "$30")
        self.assertEqual(rc, 0, out)
        by = {a["vendor"]: a for a in self.cfg().accounts}
        self.assertEqual(by["claude"]["usd_cents_month"], 10000)
        self.assertEqual(by["claude"]["price_from"], "operator")
        self.assertEqual(by["claude"]["price_for_plan"], "max/default_claude_max_5x")
        self.assertNotIn("usd_cents_month", by["codex"])
        self.assertEqual(by["grok"]["usd_cents_month"], 3000)
        snap, _ = self.h.run()
        t = tile(snap, "Committed")
        self.assertEqual((t["value"], t["kind"]), ("$130 / mo", "declared"))
        self.assertIn("plus $20 in unconfirmed list prices", t["note"])

    def test_the_catalogue_hint_is_never_written(self):
        self.seed()
        self.setup("y", "", "y", "", "y", "")
        with open(self.h.env().config) as f:
            text = f.read()
        self.assertNotIn("10000", text)
        self.assertNotIn("usd_cents_month =", text)

    def test_bad_amount_is_asked_again(self):
        self.seed()
        rc, out = self.setup("y", "a hundred", "100", "n", "n")
        self.assertIn("type an amount", out)
        self.assertEqual(self.cfg().accounts[0]["usd_cents_month"], 10000)

    def test_declining_keeps_it_proposed(self):
        self.seed()
        self.setup("n", "n", "n")
        self.assertEqual(self.cfg().accounts, [])

    def test_unknown_plan_gets_no_price(self):
        self.seed(claude_tier="default_claude_mystery")
        snap, _ = self.h.run()
        row = [r for r in table(snap, "By account")["rows"] if r[1] == "Claude"][0]
        self.assertEqual(row[3], "unreported")

    def test_vendor_plan_change_reverts_to_list_until_retyped(self):
        self.seed(codex_plan="plus")
        self.setup("n", "y", "25", "n")
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Committed")["kind"], "declared")
        self.h.write(os.path.join(self.h.codex, "b.jsonl"), [
            codex_meta("s2", "2026-10-06T00:00:00Z"),
            codex_count("2026-10-06T00:00:00Z", 1, 0, 1, rate_limits(plan="pro"))])
        snap, _ = self.h.run()
        row = [r for r in table(snap, "By account")["rows"] if r[1] == "Codex"][0]
        self.assertIn("plan changed", row[3])
        self.assertNotEqual(tile(snap, "Committed")["kind"], "declared")
        # Setup asks again; a new typed price is declared for the new plan.
        self.setup("n", "200", "n")
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Committed")["value"], "$200 / mo")

    def test_setup_is_rerunnable_and_never_deletes_an_operator_account(self):
        self.seed()
        cfg = self.cfg()
        cfg.accounts.append({"id": "my-gemini", "vendor": "gemini", "kind": "subscription",
                             "usd_cents_month": 1999, "price_from": "operator"})
        cfg.save()
        self.setup(yes=True)
        self.setup(yes=True)
        ids = [a["id"] for a in self.cfg().accounts]
        self.assertIn("my-gemini", ids)
        self.assertEqual(len(ids), len(set(ids)))
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Committed")["value"], "$19.99 / mo")

    def test_claude_login_change_is_a_new_account_from_that_day(self):
        self.seed()
        self.h.run()
        # The module notices the new login at 21:00; usage after that is
        # the new account's.
        self.h.logins(claude_fp="fp-claude-2")
        self.h.run(now=self.h.now + 3600)
        self.h.write(os.path.join(self.h.claude, "b.jsonl"),
                     [claude_line("req_2", "2026-10-07T21:30:00Z")])
        snap, _ = self.h.run(now=self.h.now + 7200)
        claude_rows = [r for r in table(snap, "By account")["rows"] if r[1] == "Claude"]
        self.assertEqual(len(claude_rows), 2)
        self.assertEqual(sorted(r[4] for r in claude_rows), ["115", "115"])

    def test_config_written_is_readable_by_lights_strict_reader(self):
        self.seed()
        self.setup("y", "19.99", "y", "", "y", "")
        with open(self.h.env().config) as f:
            text = f.read()
        tomlmini.loads_strict(text)       # the vendored copy of Light's reader
        self.assertEqual(oct(os.stat(self.h.env().config).st_mode & 0o777), "0o600")

    def test_no_terminal_and_no_yes_says_how(self):
        self.seed()
        out = io.StringIO()
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            rc = main.setup(self.h.env(), out=out)
        finally:
            sys.stdin = old
        self.assertEqual(rc, 1)
        self.assertIn("--yes", out.getvalue())


class PlanMatching(unittest.TestCase):
    def test_matching_needs_one_clear_row(self):
        plans = load_plans()
        self.assertEqual(match_plan(plans, "claude", "max", "default_claude_max_20x")["id"],
                         "claude-max-20x")
        self.assertIsNone(match_plan(plans, "claude", "max", None))
        self.assertIsNone(match_plan(plans, "claude", None, None))
        self.assertIsNone(match_plan(plans, "codex", "pro", None))
        self.assertEqual(match_plan(plans, "codex", "plus", None)["usd_cents_month"], 2000)

    def test_parse_money(self):
        self.assertEqual(main.parse_money("$1,200.5"), 120050)
        self.assertEqual(main.parse_money("20"), 2000)
        self.assertIsNone(main.parse_money("-5"))
        self.assertIsNone(main.parse_money("1e3"))


if __name__ == "__main__":
    unittest.main()
