"""Billing fetchers against a local stub of Light's fetch proxy (plan §8.2
"Fetchers"): success, pagination to the end, partial failures that store
nothing, 401, 429, timeouts. FinOps holds no key (Light plan §6.7.2): the
stub checks that no request carries a credential, and plays the proxy,
which in Light adds it."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import helpers  # noqa: F401  (sys.path)
from finops.fetch import anthropic, gcp, openai, xai
from finops.fetch.http import Client, FetchError

START, END = date(2026, 9, 1), date(2026, 10, 9)
CREDENTIAL_HEADERS = ("authorization", "x-api-key", "proxy-authorization", "cookie")


class Stub:
    """Light's fetch proxy, played by a local server: requests arrive in
    proxy form (an absolute https URL). `routes(method, host, path, query,
    headers, body)` returns (status, json or raw text, headers)."""

    def __init__(self, routes):
        stub = self
        self.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                u = urlsplit(self.path)
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                stub.seen.append((method, u.scheme, u.hostname, u.path, parse_qs(u.query), hdrs,
                                  body))
                status, doc, extra = routes(method, u.hostname, u.path, parse_qs(u.query),
                                            hdrs, body)
                raw = doc.encode() if isinstance(doc, str) else json.dumps(doc).encode()
                self.send_response(status)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class StubCase(unittest.TestCase):
    hosts = ("api.anthropic.com", "api.openai.com", "management-api.x.ai",
             "bigquery.googleapis.com")

    def stub(self, routes):
        s = Stub(routes)
        self.addCleanup(s.close)
        self.addCleanup(self.no_credentials, s)
        self.s = s
        return s

    def no_credentials(self, s):
        for _method, scheme, host, path, _q, hdrs, _b in s.seen:
            self.assertEqual(scheme, "https", path)
            for h in CREDENTIAL_HEADERS:
                self.assertNotIn(h, hdrs, f"FinOps sent {h} to {host}{path}")

    def client(self, timeout=10):
        return Client(self.hosts, self.s.api, timeout=timeout)


class TheClient(unittest.TestCase):
    def test_hosts_and_schemes(self):
        c = Client(["api.anthropic.com"], "http://127.0.0.1:9")
        for url in ("https://evil.api.anthropic.com/x", "https://api.openai.com/x",
                    "http://api.anthropic.com/x"):
            with self.assertRaises(FetchError, msg=url):
                c.json("GET", url)

    def test_no_proxy_means_an_older_light(self):
        for api in (None, "", "https://127.0.0.1:1", "http://127.0.0.1"):
            with self.assertRaisesRegex(FetchError, "update Light"):
                Client(["api.anthropic.com"], api)


class Anthropic(StubCase):
    def routes(self, fail_page=None, status=200):
        def r(method, host, path, q, h, body):
            assert host == "api.anthropic.com" and h.get("anthropic-version") == "2023-06-01"
            if path == "/v1/organizations/me":
                return 200, {"id": "org-1", "name": "Fixture Org", "type": "organization"}, {}
            if status != 200:
                return status, {"type": "error", "error": {"type": "rate_limit_error"}}, \
                    {"retry-after": "30"}
            page = (q.get("page") or [""])[0]
            if page == fail_page:
                return 500, {"type": "error", "error": {"type": "api_error"}}, {}
            if not page:
                return 200, {"data": [
                    {"starting_at": "2026-09-01T00:00:00Z", "ending_at": "2026-09-02T00:00:00Z",
                     "results": [{"amount": "12345.67", "currency": "USD"},
                                 {"amount": "55", "currency": "USD"}]},
                    {"starting_at": "2026-09-02T00:00:00Z", "ending_at": "2026-09-03T00:00:00Z",
                     "results": []}], "has_more": True, "next_page": "p2"}, {}
            return 200, {"data": [
                {"starting_at": "2026-10-08T00:00:00Z", "ending_at": "2026-10-09T00:00:00Z",
                 "results": [{"amount": "1", "currency": "USD"}]}],
                "has_more": False, "next_page": None}, {}
        return r

    def test_pages_to_the_end_in_dollars(self):
        s = self.stub(self.routes())
        doc = anthropic.fetch(self.client(), START, END)
        self.assertEqual(doc["days"], {"2026-09-01": {"USD": "124.0067"},
                                       "2026-10-08": {"USD": "0.01"}})
        self.assertEqual(doc["org"], {"id": "org-1", "name": "Fixture Org"})
        q = [x[4] for x in s.seen if x[3].endswith("cost_report")]
        self.assertEqual(q[0]["starting_at"], ["2026-09-01T00:00:00Z"])
        self.assertEqual(q[0]["bucket_width"], ["1d"])
        self.assertEqual(q[1]["page"], ["p2"])

    def test_a_failed_page_returns_nothing(self):
        self.stub(self.routes(fail_page="p2"))
        with self.assertRaises(FetchError) as cm:
            anthropic.fetch(self.client(), START, END)
        self.assertEqual(cm.exception.status, 500)

    def test_401_and_429(self):
        self.stub(lambda m, host, p, q, h, b: (401, {"type": "error", "error": {
            "type": "authentication_error"}}, {}))
        with self.assertRaises(FetchError) as cm:
            anthropic.fetch(self.client(), START, END)
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("the key was refused", str(cm.exception))
        self.stub(self.routes(status=429))
        with self.assertRaises(FetchError) as cm:
            anthropic.fetch(self.client(), START, END)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, "30"))

    def test_a_proxy_refusal_is_said(self):
        self.stub(lambda m, host, p, q, h, b: (403, "not one of this grant's vendor hosts", {}))
        with self.assertRaisesRegex(FetchError, r"refused \(403\) \[not one of this grant"):
            anthropic.fetch(self.client(), START, END)

    def test_has_more_without_a_page_is_an_error(self):
        def r(method, host, path, q, h, body):
            if path.endswith("/me"):
                return 404, {}, {}
            return 200, {"data": [], "has_more": True, "next_page": None}, {}
        self.stub(r)
        with self.assertRaises(ValueError):
            anthropic.fetch(self.client(), START, END)

    def test_timeout(self):
        def r(method, host, path, q, h, body):
            time.sleep(3)
            return 200, {}, {}
        self.stub(r)
        with self.assertRaises(FetchError):
            anthropic.fetch(self.client(timeout=1), START, END)


class OpenAI(StubCase):
    def test_pages_and_currency(self):
        def r(method, host, path, q, h, body):
            assert host == "api.openai.com"
            page = (q.get("page") or [""])[0]
            b = {"object": "bucket", "start_time": 1788220800 if not page else 1791417600,
                 "end_time": 0, "results": [
                     {"object": "organization.costs.result",
                      "amount": {"value": 0.06, "currency": "usd"}},
                     {"object": "organization.costs.result",
                      "amount": {"value": 1.5, "currency": "usd"}}]}
            return 200, {"object": "page", "data": [b], "has_more": not page,
                         "next_page": None if page else "cur2"}, {}
        s = self.stub(r)
        doc = openai.fetch(self.client(), START, END)
        self.assertEqual(doc["days"], {"2026-09-01": {"USD": "1.56"},
                                       "2026-10-08": {"USD": "1.56"}})
        q = s.seen[0][4]
        self.assertEqual((q["start_time"], q["bucket_width"]), (["1788220800"], ["1d"]))


class XAI(StubCase):
    def routes(self, scope="SCOPE_TEAM", limit=False):
        def r(method, host, path, q, h, body):
            assert host == "management-api.x.ai"
            if path == "/auth/management-keys/validation":
                return 200, {"scope": scope, "scopeId": "team-123", "teamId": "team-123"}, {}
            if path == "/v1/billing/teams/team-123/usage" and method == "POST":
                req = json.loads(body)["analyticsRequest"]
                assert req["timeUnit"] == "TIME_UNIT_DAY"
                return 200, {"limitReached": limit, "timeSeries": [
                    {"group": ["grok-4"], "dataPoints": [
                        {"timestamp": "2026-10-01T00:00:00Z", "values": [0.75973725]},
                        {"timestamp": "2026-10-02T00:00:00Z", "values": [0]}]},
                    {"group": ["grok-code"], "dataPoints": [
                        {"timestamp": "2026-10-01T00:00:00Z", "values": [0.25]}]}]}, {}
            return 404, {}, {}
        return r

    def test_team_spend(self):
        self.stub(self.routes())
        doc = xai.fetch(self.client(), START, END)
        self.assertEqual(doc["days"], {"2026-10-01": {"USD": "1.00973725"},
                                       "2026-10-02": {"USD": "0"}})
        self.assertIn("unit", doc["notes"][0])

    def test_refusals(self):
        self.stub(self.routes(scope="SCOPE_ORGANIZATION"))
        with self.assertRaises(ValueError):
            xai.fetch(self.client(), START, END)
        self.stub(self.routes(limit=True))
        with self.assertRaises(ValueError):
            xai.fetch(self.client(), START, END)


class GCP(StubCase):
    def routes(self):
        def r(method, host, path, q, h, body):
            assert host == "bigquery.googleapis.com"
            if method == "POST" and path == "/bigquery/v2/projects/my-proj-1/queries":
                b = json.loads(body)
                assert b["useLegacySql"] is False and \
                    "`my-proj-1.billing.gcp_billing_export_v1_X`" in b["query"]
                return 200, {"jobComplete": False, "jobReference": {
                    "projectId": "my-proj-1", "jobId": "job_1", "location": "US"}}, {}
            if path == "/bigquery/v2/projects/my-proj-1/queries/job_1":
                if not q.get("pageToken"):
                    return 200, {"jobComplete": True, "pageToken": "t2",
                                 "jobReference": {"jobId": "job_1", "location": "US"},
                                 "rows": [{"f": [{"v": "2026-10-01"}, {"v": "USD"},
                                                 {"v": "10.5"}]}]}, {}
                return 200, {"jobComplete": True, "jobReference": {"jobId": "job_1"},
                             "rows": [{"f": [{"v": "2026-10-02"}, {"v": "EUR"},
                                             {"v": "-0.25"}]}]}, {}
            return 404, {}, {}
        return r

    def test_query_poll_and_pages(self):
        self.stub(self.routes())
        doc = gcp.fetch(self.client(), START, END,
                        {"table": "my-proj-1.billing.gcp_billing_export_v1_X"})
        self.assertEqual(doc["days"], {"2026-10-01": {"USD": "10.5"},
                                       "2026-10-02": {"EUR": "-0.25"}})

    def test_bad_params(self):
        self.stub(self.routes())
        for p in ({}, {"table": "x"}, {"table": "p.d.t`; DROP"},
                  {"table": "my-proj-1.d.t", "location": "us central"}):
            with self.assertRaises(ValueError, msg=p):
                gcp.fetch(self.client(), START, END, p)


class Window(unittest.TestCase):
    def test_last_month_to_tomorrow(self):
        from finops.fetch.common import window
        self.assertEqual(window(1791466622), (date(2026, 9, 1), date(2026, 10, 9)))
        self.assertEqual(window(1767225600 + 3600), (date(2025, 12, 1), date(2026, 1, 2)))

    def test_amounts_are_exact(self):
        from finops.fetch.common import dec
        self.assertEqual(dec("12345.67", 100), Decimal("123.4567"))
        for bad in (None, True, "x", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                dec(bad)


class TheEntry(StubCase):
    """fetcher.run(): vendor, hosts, params and the proxy come from the
    environment Light sets; there is no key to read."""

    def test_run_from_the_environment(self):
        from finops import fetch
        self.stub(Anthropic.routes(Anthropic()))
        env = {"CORRAL_FETCH_VENDOR": "anthropic", "CORRAL_FETCH_API": self.s.api,
               "CORRAL_FETCH_HOSTS": "api.anthropic.com", "CORRAL_FETCH_PARAM_TABLE": "x"}
        doc = fetch.run(env, now=1791466622)
        self.assertEqual((doc["schema"], doc["vendor"], doc["range"]),
                         ("finops.billed/1", "anthropic",
                          {"start": "2026-09-01", "end": "2026-10-09"}))
        self.assertGreaterEqual(doc["requests"], 3)
        self.assertEqual(fetch.params_from(env), {"table": "x"})
        with self.assertRaises(FetchError):
            fetch.run(dict(env, CORRAL_FETCH_VENDOR="evil"))
        with self.assertRaises(FetchError):           # a host not granted
            fetch.run(dict(env, CORRAL_FETCH_HOSTS="api.openai.com"), now=1791466622)
        with self.assertRaisesRegex(FetchError, "update Light"):
            fetch.run({k: v for k, v in env.items() if k != "CORRAL_FETCH_API"})

    def test_no_key_is_read(self):
        from finops import fetch
        self.stub(Anthropic.routes(Anthropic()))
        d = tempfile.mkdtemp(prefix="finops-entry-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        env = {"CORRAL_FETCH_VENDOR": "anthropic", "CORRAL_FETCH_API": self.s.api,
               "CORRAL_FETCH_HOSTS": "api.anthropic.com",
               "CORRAL_FETCH_KEY": os.path.join(d, "absent")}
        fetch.run(env, now=1791466622)           # an old variable is simply ignored


class PanelFixes(StubCase):
    """Findings of the 2026-10-08 panel on Phase 4."""

    def test_malformed_openai_results_fail_the_fetch(self):
        for bad in ([{"start_time": 1788220800, "results": [{}]}], ["not a bucket"],
                    [{"start_time": 1788220800}]):
            self.stub(lambda m, host, p, q, h, b, bad=bad: (200, {"data": bad,
                                                                  "has_more": False}, {}))
            with self.assertRaises(ValueError, msg=bad):
                openai.fetch(self.client(), START, END)

    def test_amounts_keep_every_cent(self):
        raw = ('{"data": [{"start_time": 1788220800, "results": [{"amount": '
               '{"value": 90071992547409.91, "currency": "usd"}}]}], "has_more": false}')
        self.stub(lambda m, host, p, q, h, b: (200, raw, {}))
        doc = openai.fetch(self.client(), START, END)
        self.assertEqual(doc["days"]["2026-09-01"]["USD"], "90071992547409.91")

    def test_an_odd_xai_team_id_is_refused(self):
        def r(method, host, path, q, h, body):
            if path == "/auth/management-keys/validation":
                return 200, {"scope": "SCOPE_TEAM", "scopeId": "abc?x=1"}, {}
            return 404, {}, {}
        self.stub(r)
        with self.assertRaises(ValueError):
            xai.fetch(self.client(), START, END)


if __name__ == "__main__":
    unittest.main()
