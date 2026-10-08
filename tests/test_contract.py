"""The contract with Light, privacy, the entry points as Light runs them."""
import ast
import json
import os
import resource
import subprocess
import sys
import unittest

import light_contract
from helpers import (ROOT, SENTINEL, HostCase, claude_line, codex_count, codex_message,
                     codex_meta, codex_record, gemini_db, grok_report, rate_limits, ts,
                     user_line)

TITLE_SENTINEL = "SENTINEL-TITLE-55e1"


def populate(h):
    at = "2026-10-06T00:00:00Z"
    h.logins()
    h.write(os.path.join(h.claude, "a.jsonl"), [user_line(at), claude_line("req_1", at),
                                                 claude_line(None, at)])
    h.write(os.path.join(h.panes, "sess-a.jsonl"), [claude_line("req_2", at)])
    h.write(os.path.join(h.codex, "a.jsonl"), [
        codex_meta("cx-1", at), codex_message(at), codex_record("r1", at, 100, 10, 5, "cx-1"),
        codex_count(at, 100, 10, 5, rate_limits(30.0, int(h.now + 3600), 5.0,
                                                int(h.now + 86400)))])
    gemini_db(os.path.join(h.gemini, "55555555-eeee.db"), "55555555-eeee",
              [(1, ts(at), 100, 0, 3, 4, "gemini-3.8-flash")]).close()
    h.grok(grok_report("aaaaaaaa-1111", [(1, "2026-10-06T00:00:00", 10, 10, 10 ** 10)]))
    h.feed_json("panes.json", {"schema": "corral-light.module-feed/1", "panes": [
        {"id": "p1", "agent": "claude", "acp_session": "sess-a", "title": TITLE_SENTINEL,
         "origin": "human", "usage": [], "segments": []},
        {"id": "p2", "agent": "codex", "acp_session": "cx-1", "title": "Codex",
         "origin": "consult", "worktree_id": "wt-1", "usage": [], "segments": []}]})
    h.feed_json("quota.json", {"schema": "corral-light.module-feed/1", "accounts": {
        "fp-claude-1": {"lane": "claude", "windows": {"five_hour": {
            "utilization": 0.2, "status": "allowed", "resets_at_s": h.now + 3600,
            "observed_at": h.now - 60}}}}})
    h.feed_json("host.json", {"schema": "corral-light.module-feed/1", "platform": "linux",
                              "timezone": {"name": "UTC"}})


class Contract(HostCase):
    def test_snapshots_validate_with_lights_validator_and_nothing_is_dropped(self):
        for setup in (lambda h: None, populate):
            setup(self.h)
            snap, _ = self.h.run()
            raw = json.dumps(snap).encode()
            v, err = light_contract.validate_snapshot(raw, notices=True)
            self.assertIsNone(err)
            self.assertTrue(v["ok"])
            self.assertNotIn("notices_dropped", v)
            self.assertEqual([x["id"] for x in v["notices"]],
                             [x["id"] for x in snap["notices"]])
            self.assertFalse([b for b in v["view"] if "dropped" in b or b["type"] ==
                              "unsupported"])
            for b in snap["view"]:
                for it in b.get("items", []):
                    self.assertIn(it["kind"], light_contract.KINDS)
                    self.assertIn(it["level"], light_contract.LEVELS)
                if b["type"] == "link":
                    self.assertTrue(light_contract.safe_https_url(b["url"]))

    def test_manifest_validates_against_the_files(self):
        with open(os.path.join(ROOT, "module.json")) as f:
            m = light_contract.validate_manifest(json.load(f), ROOT)
        self.assertEqual(m["network"], "none")
        self.assertEqual(m["vendor_reports"], ["grok-usage"])
        self.assertIs(m["notices"], True)
        self.assertEqual(m["fetcher"]["vendors"], ["anthropic", "gcp", "openai", "xai"])
        self.assertEqual(m["fetcher"]["script"], "fetcher.py")
        self.assertLessEqual(m["collector"]["budget_s"], m["collector"]["timeout_s"])

    def test_most_used_joins_panes_and_groups(self):
        populate(self.h)
        snap, _ = self.h.run()
        t = next(b for b in snap["view"] if b["type"] == "table" and b["title"].startswith("Most"))
        names = [r[0] for r in t["rows"]]
        self.assertTrue(any(n.startswith(TITLE_SENTINEL) for n in names))
        self.assertIn("worktree wt-1", names)
        self.assertIn("all consult panes", names)


class Privacy(HostCase):
    def test_sentinels_never_reach_the_ledger_and_prompts_never_the_output(self):
        populate(self.h)
        snap, _ = self.h.run()
        blobs = b""
        for name in os.listdir(self.h.data):
            p = os.path.join(self.h.data, name)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    blobs += f.read()
        self.assertNotIn(SENTINEL.encode(), blobs)
        self.assertNotIn(TITLE_SENTINEL.encode(), blobs)
        self.assertNotIn(SENTINEL, json.dumps(snap))
        env = run_env(self.h)
        for args in (["show"], ["show", "--json"], ["accounts"], ["doctor"]):
            r = subprocess.run([sys.executable, "-I", "-B", os.path.join(ROOT, "cli.py")] + args,
                               env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(SENTINEL, r.stdout + r.stderr)


def run_env(h, **extra):
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC", "HOME": h.data,
           "CORRAL_MODULE_API": "1", "CORRAL_MODULE_DATA": h.data,
           "CORRAL_MODULE_CONFIG": os.path.join(h.cfgdir, "config.toml"),
           "CORRAL_MODULE_FEED": h.feed, "CORRAL_MODULE_SANDBOXED": "1",
           "CORRAL_READ_CLAUDE_PROJECTS": os.pathsep.join(h.env().reads["claude-projects"]),
           "CORRAL_READ_CODEX_SESSIONS": os.path.join(h.root, "codex/sessions"),
           "CORRAL_READ_GEMINI_STORE": h.gemini}
    env.update(extra)
    return env


class EntryPoints(HostCase):
    def collector(self, env, budget="5"):
        return subprocess.run([sys.executable, "-I", "-B", os.path.join(ROOT, "collector.py"),
                               "snapshot", "--budget", budget], env=env, capture_output=True,
                              timeout=120)

    def test_collector_as_light_runs_it(self):
        populate(self.h)
        r = self.collector(run_env(self.h))
        self.assertEqual(r.returncode, 0, r.stderr)
        snap, err = light_contract.validate_snapshot(r.stdout)
        self.assertIsNone(err)
        self.assertLess(len(r.stdout), light_contract.STDOUT_CAP)
        self.assertEqual(r.stderr, b"")

    def test_failure_exits_non_zero_with_one_short_line(self):
        populate(self.h)
        bad = run_env(self.h, CORRAL_MODULE_DATA="/proc/finops-cannot-write-here")
        r = self.collector(bad)
        self.assertNotEqual(r.returncode, 0)
        lines = r.stderr.decode().strip().splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertNotIn("Traceback", r.stderr.decode())
        self.assertEqual(r.stdout, b"")

    def test_peak_memory_over_a_large_history(self):
        size_mb = int(os.environ.get("FINOPS_BIG_MB", "40"))
        line = claude_line("x", "2026-10-06T00:00:00Z") + "\n"
        per_file = (4 << 20) // len(line)
        n_files = max(1, size_mb // 4)
        for fi in range(n_files):
            with open(os.path.join(self.h.claude, f"big{fi}.jsonl"), "w") as f:
                for i in range(per_file):
                    f.write(claude_line(f"r{fi}_{i}", "2026-10-06T00:00:00Z") + "\n")
        env = run_env(self.h)
        for _ in range(50):
            r = self.collector(env, budget="2")
            self.assertEqual(r.returncode, 0, r.stderr)
            if b'"progress"' not in r.stdout:
                break
        else:
            self.fail("backfill did not finish")
        peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
        self.assertLess(peak_mb, 200)


class Portability(unittest.TestCase):
    def test_every_file_parses_as_python_3_9(self):
        for dirpath, _dirs, files in os.walk(ROOT):
            if ".git" in dirpath:
                continue
            for fn in files:
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    with open(p, encoding="utf-8") as f:
                        ast.parse(f.read(), p, feature_version=(3, 9))

    def test_no_fstring_needs_python_3_12(self):
        """Python 3.12 relaxed f-strings: a quote matching the outer one, or a
        backslash, inside a replacement field fails on 3.9. The 3.9 grammar
        flag above does not catch these, so the tokens are checked here."""
        import io
        import tokenize
        if not hasattr(tokenize, "FSTRING_START"):
            self.skipTest("this Python parses f-strings the old way; it would fail on import")
        bad = []
        for dirpath, _dirs, files in os.walk(ROOT):
            if ".git" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    toks = list(tokenize.generate_tokens(io.StringIO(f.read()).readline))
                stack = []
                for t in toks:
                    if t.type == tokenize.FSTRING_START:
                        if stack:
                            q = t.string.lstrip("rRbBfF")[:1]
                            if q in stack:
                                bad.append(f"{fn}:{t.start[0]} nested quote")
                        stack.append(t.string.lstrip("rRbBfF")[:1])
                    elif t.type == tokenize.FSTRING_END:
                        stack.pop()
                    elif stack and t.type == tokenize.STRING:
                        q = t.string.lstrip("rRbBuUfF")[:1]
                        if q in stack or "\\" in t.string:
                            bad.append(f"{fn}:{t.start[0]} quote or backslash in a field")
        self.assertEqual(bad, [])

    def test_no_file_python_would_run_on_its_own(self):
        for dirpath, _dirs, files in os.walk(ROOT):
            if ".git" in dirpath:
                continue
            for fn in files:
                self.assertFalse(fn.endswith(".pth"))
                self.assertNotIn(fn.split(".", 1)[0], light_contract_forbidden())


def light_contract_forbidden():
    return ("sitecustomize", "usercustomize")


if __name__ == "__main__":
    unittest.main()
