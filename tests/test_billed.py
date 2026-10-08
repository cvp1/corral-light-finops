"""Billing API results in the ledger and the dialog (plan §6.7, §8.2):
a newer complete fetch replaces only the days in its range, an older one
is ignored, amounts stay exact, and billed figures never join Committed."""
import json
import os

from helpers import HostCase, notes, table, tile
from light_contract import validate_snapshot
from finops.sources import billed


def stored(key="anthropic-admin", vendor="anthropic", at="2026-10-07T12:00:00Z",
           start="2026-09-01", end="2026-10-08", days=None, org=None):
    return {"schema": "corral-light.fetch-result/1", "module": "finops", "key": key,
            "vendor": vendor, "fetched_at": at,
            "result": {"schema": "finops.billed/1", "vendor": vendor,
                       "org": org if org is not None else {"id": "org-1", "name": "Fixture Org"},
                       "range": {"start": start, "end": end},
                       "days": days if days is not None else {
                           "2026-09-15": {"USD": "100.10"}, "2026-10-01": {"USD": "12.3456"},
                           "2026-10-07": {"USD": "0.0044"}},
                       "notes": ["a vendor note"]}}


class Billed(HostCase):
    def put(self, doc, name=None):
        with open(os.path.join(self.h.fetched, (name or doc["key"]) + ".json"), "w") as f:
            json.dump(doc, f)

    def test_tile_is_billed_exact_and_apart_from_committed(self):
        self.put(stored())
        snap, _ = self.h.run()
        t = tile(snap, "Billed: Fixture Org")
        self.assertEqual((t["value"], t["kind"]), ("$12.35", "billed"))
        self.assertIn("never part of Committed", t["note"])
        self.assertEqual(tile(snap, "Committed")["kind"], "unknown")
        tb = table(snap, "Billing APIs")
        self.assertEqual(tb["rows"][0][:4], ["Fixture Org", "Anthropic", "$12.35", "$100.10"])
        self.assertIn("a vendor note", notes(snap))
        v, err = validate_snapshot(json.dumps(snap), notices=True)
        self.assertIsNone(err)
        self.assertFalse([b for b in v["view"] if "dropped" in b])

    def test_newer_replaces_its_range_only_and_older_is_ignored(self):
        self.put(stored())
        self.h.run()
        self.put(stored(at="2026-10-07T18:00:00Z", start="2026-10-01", end="2026-10-08",
                        days={"2026-10-01": {"USD": "20"}}))
        snap, _ = self.h.run()
        led = self.h.ledger()
        try:
            rows = dict(led.q("SELECT day, amount FROM billed_day ORDER BY day"))
        finally:
            led.close()
        self.assertEqual(rows, {"2026-09-15": "100.10", "2026-10-01": "20"})
        self.put(stored(at="2026-10-01T00:00:00Z", days={"2026-10-01": {"USD": "999"}}))
        self.h.run()
        led = self.h.ledger()
        try:
            self.assertEqual(led.one("SELECT amount FROM billed_day WHERE day='2026-10-01'"),
                             "20")
        finally:
            led.close()

    def test_other_currencies_and_names(self):
        self.put(stored(key="gcp-billing", vendor="gcp", org={"id": "my-proj", "name": ""},
                        days={"2026-10-02": {"EUR": "7.5", "USD": "1"}}))
        snap, _ = self.h.run()
        self.assertEqual(tile(snap, "Billed: Google Cloud API (gcp-billing)")["value"], "$1.00 + 7.50 EUR")

    def test_bad_results_are_noted_not_stored(self):
        bad = [
            stored(days={"2026-12-01": {"USD": "1"}}),           # outside its range
            stored(days={"2026-10-01": {"usd": "1"}}),           # lowercase currency
            stored(days={"2026-10-01": {"USD": "1e9"}}),         # not plain decimal
            stored(days={"2026-10-01": {"USD": 1.5}}),           # a number, not text
            dict(stored(), schema="x"), dict(stored(), key="../x"),
            dict(stored(), vendor="evil")]
        for i, b in enumerate(bad):
            self.put(b, name=f"bad{i}")
        with open(os.path.join(self.h.fetched, "junk.json"), "w") as f:
            f.write("{not json")
        snap, _ = self.h.run()
        led = self.h.ledger()
        try:
            self.assertEqual(led.one("SELECT COUNT(*) FROM billed_day"), 0)
        finally:
            led.close()
        self.assertEqual(sum("could not be read" in n for n in notes(snap)), len(bad) + 1)

    def test_parse_maps_the_key_to_an_api_account(self):
        acct, vendor, org, *_ = billed.parse(stored())
        self.assertEqual((acct, vendor, org["name"]), ("api:anthropic-admin", "anthropic",
                                                       "Fixture Org"))

    def test_no_fetched_dir_is_fine(self):
        self.h.fetched = None
        snap, _ = self.h.run()
        self.assertFalse([b for b in snap["view"] if b.get("title", "").startswith("Billing")])
