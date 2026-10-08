"""Billing fetchers against a local stub (plan §8.2 "Fetchers"): success,
pagination to the end, partial failures that store nothing, 401, 429,
timeouts, and the GCP token's RSA signature."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import helpers  # noqa: F401  (sys.path)
from finops.fetch import anthropic, gcp, openai, rsa, xai
from finops.fetch.http import Client, FetchError

KEY = "sk-test-FIXTURE-0123456789abcdefghijklmnop"
START, END = date(2026, 9, 1), date(2026, 10, 9)


class Stub:
    """A local HTTP server; `routes(method, path, query, headers, body)`
    returns (status, json, headers) or raises to drop the connection."""

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
                stub.seen.append((method, u.path, parse_qs(u.query), dict(self.headers), body))
                status, doc, hdrs = routes(method, u.path, parse_qs(u.query),
                                           self.headers, body)
                raw = json.dumps(doc).encode()
                self.send_response(status)
                for k, v in (hdrs or {}).items():
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
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class StubCase(unittest.TestCase):
    vendor = None

    def stub(self, routes):
        s = Stub(routes)
        self.addCleanup(s.close)
        for mod, attr in ((anthropic, "BASE"), (openai, "BASE"), (xai, "BASE")):
            old = getattr(mod, attr)
            setattr(mod, attr, s.base)
            self.addCleanup(setattr, mod, attr, old)
        for attr, path in (("TOKEN_URL", "/token"), ("BQ", "/bigquery/v2")):
            old = getattr(gcp, attr)
            setattr(gcp, attr, s.base + path)
            self.addCleanup(setattr, gcp, attr, old)
        return s

    def client(self, timeout=10):
        return Client(["127.0.0.1"], timeout=timeout)


class TheClient(unittest.TestCase):
    def test_hosts_and_schemes(self):
        c = Client(["api.anthropic.com"])
        for url in ("https://evil.api.anthropic.com/x", "https://api.openai.com/x",
                    "http://api.anthropic.com/x"):
            with self.assertRaises(FetchError, msg=url):
                c.json("GET", url)


class Anthropic(StubCase):
    def routes(self, fail_page=None, status=200):
        def r(method, path, q, h, body):
            if h.get("x-api-key") != KEY or h.get("anthropic-version") != "2023-06-01":
                return 401, {"type": "error", "error": {"type": "authentication_error"}}, {}
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
        doc = anthropic.fetch(self.client(), KEY, START, END)
        self.assertEqual(doc["days"], {"2026-09-01": {"USD": "124.0067"},
                                       "2026-10-08": {"USD": "0.01"}})
        self.assertEqual(doc["org"], {"id": "org-1", "name": "Fixture Org"})
        q = [x[2] for x in s.seen if x[1].endswith("cost_report")]
        self.assertEqual(q[0]["starting_at"], ["2026-09-01T00:00:00Z"])
        self.assertEqual(q[0]["bucket_width"], ["1d"])
        self.assertEqual(q[1]["page"], ["p2"])
        self.assertNotIn(KEY, json.dumps(doc))

    def test_a_failed_page_returns_nothing(self):
        self.stub(self.routes(fail_page="p2"))
        with self.assertRaises(FetchError) as cm:
            anthropic.fetch(self.client(), KEY, START, END)
        self.assertEqual(cm.exception.status, 500)

    def test_401_and_429(self):
        self.stub(self.routes())
        with self.assertRaises(FetchError) as cm:
            anthropic.fetch(self.client(), "wrong-key", START, END)
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn("wrong-key", str(cm.exception))
        self.stub(self.routes(status=429))
        with self.assertRaises(FetchError) as cm:
            anthropic.fetch(self.client(), KEY, START, END)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, "30"))
        self.assertIn("rate limited", str(cm.exception))

    def test_has_more_without_a_page_is_an_error(self):
        def r(method, path, q, h, body):
            if path.endswith("/me"):
                return 404, {}, {}
            return 200, {"data": [], "has_more": True, "next_page": None}, {}
        self.stub(r)
        with self.assertRaises(ValueError):
            anthropic.fetch(self.client(), KEY, START, END)

    def test_timeout(self):
        import time

        def r(method, path, q, h, body):
            time.sleep(3)
            return 200, {}, {}
        self.stub(r)
        with self.assertRaises(FetchError):
            anthropic.fetch(self.client(timeout=1), KEY, START, END)


class OpenAI(StubCase):
    def test_pages_and_currency(self):
        def r(method, path, q, h, body):
            if h.get("Authorization") != f"Bearer {KEY}":
                return 401, {"error": {"type": "invalid_request_error"}}, {}
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
        doc = openai.fetch(self.client(), KEY, START, END)
        self.assertEqual(doc["days"], {"2026-09-01": {"USD": "1.56"},
                                       "2026-10-08": {"USD": "1.56"}})
        q = s.seen[0][2]
        self.assertEqual((q["start_time"], q["bucket_width"]), (["1788220800"], ["1d"]))


class XAI(StubCase):
    def routes(self, scope="SCOPE_TEAM", limit=False):
        def r(method, path, q, h, body):
            if h.get("Authorization") != f"Bearer {KEY}":
                return 401, {}, {}
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
        doc = xai.fetch(self.client(), KEY, START, END)
        self.assertEqual(doc["days"], {"2026-10-01": {"USD": "1.00973725"},
                                       "2026-10-02": {"USD": "0"}})
        self.assertIn("unit", doc["notes"][0])

    def test_refusals(self):
        self.stub(self.routes(scope="SCOPE_ORGANIZATION"))
        with self.assertRaises(ValueError):
            xai.fetch(self.client(), KEY, START, END)
        self.stub(self.routes(limit=True))
        with self.assertRaises(ValueError):
            xai.fetch(self.client(), KEY, START, END)


def make_sa():
    """A throwaway RSA key and service account JSON, or None without openssl."""
    if not shutil.which("openssl"):
        return None, None
    d = tempfile.mkdtemp(prefix="finops-rsa-")
    k = os.path.join(d, "k.pem")
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", k], check=True, capture_output=True)
    with open(k) as f:
        pem = f.read()
    return d, {"type": "service_account", "client_email": "fx@p.iam.gserviceaccount.com",
               "private_key": pem, "private_key_id": "abcdef0123456789abcd"}


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make and verify a key")
class TheSignature(unittest.TestCase):
    def setUp(self):
        self.dir, self.sa = make_sa()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def verify(self, msg, sig):
        k = os.path.join(self.dir, "k.pem")
        pub = os.path.join(self.dir, "pub.pem")
        subprocess.run(["openssl", "pkey", "-in", k, "-pubout", "-out", pub], check=True,
                       capture_output=True)
        for name, data in (("m", msg), ("s", sig)):
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(data)
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature",
                            os.path.join(self.dir, "s"), os.path.join(self.dir, "m")],
                           capture_output=True, text=True)
        return r.returncode == 0

    def test_openssl_verifies_our_signature(self):
        for msg in (b"", b"hello", os.urandom(1000)):
            self.assertTrue(self.verify(msg, rsa.sign(self.sa["private_key"], msg)))
        self.assertFalse(self.verify(b"other", rsa.sign(self.sa["private_key"], b"hello")))

    def test_pkcs1_pem_too(self):
        k1 = os.path.join(self.dir, "k1.pem")
        r = subprocess.run(["openssl", "pkey", "-in", os.path.join(self.dir, "k.pem"),
                            "-traditional", "-out", k1], capture_output=True)
        if r.returncode != 0:
            self.skipTest("this openssl cannot write a traditional key")
        with open(k1) as f:
            pem = f.read()
        if "BEGIN RSA PRIVATE KEY" not in pem:
            self.skipTest("this openssl wrote PKCS#8 anyway")
        self.assertTrue(self.verify(b"x", rsa.sign(pem, b"x")))

    def test_jwt_assertion(self):
        jwt = gcp.assertion(self.sa, 1_800_000_000)
        head, claims, sig = jwt.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        c = json.loads(base64.urlsafe_b64decode(pad(claims)))
        self.assertEqual((c["iss"], c["aud"], c["exp"] - c["iat"], c["scope"]),
                         (self.sa["client_email"], gcp.TOKEN_URL, 3600, gcp.SCOPE))
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head)))["alg"], "RS256")
        self.assertTrue(self.verify(f"{head}.{claims}".encode(),
                                    base64.urlsafe_b64decode(pad(sig))))


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a key")
class GCP(StubCase):
    def setUp(self):
        self.dir, self.sa = make_sa()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def routes(self):
        def r(method, path, q, h, body):
            if path == "/token":
                f = parse_qs(body.decode())
                assert f["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
                return 200, {"access_token": "ya29.TOKEN", "token_type": "Bearer",
                             "expires_in": 3600}, {}
            if h.get("Authorization") != "Bearer ya29.TOKEN":
                return 401, {}, {}
            if method == "POST" and path == "/bigquery/v2/projects/my-proj-1/queries":
                b = json.loads(body)
                assert b["useLegacySql"] is False and "`my-proj-1.billing.gcp_billing_export_v1_X`" \
                    in b["query"]
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

    def test_token_query_poll_and_pages(self):
        self.stub(self.routes())
        doc = gcp.fetch(self.client(), json.dumps(self.sa), START, END,
                        {"table": "my-proj-1.billing.gcp_billing_export_v1_X"})
        self.assertEqual(doc["days"], {"2026-10-01": {"USD": "10.5"},
                                       "2026-10-02": {"EUR": "-0.25"}})
        self.assertNotIn(self.sa["private_key"][40:80], json.dumps(doc))

    def test_bad_params_and_keys(self):
        self.stub(self.routes())
        for p in ({}, {"table": "x"}, {"table": "p.d.t`; DROP"},
                  {"table": "my-proj-1.d.t", "location": "us central"}):
            with self.assertRaises(ValueError, msg=p):
                gcp.fetch(self.client(), json.dumps(self.sa), START, END, p)
        with self.assertRaises(ValueError):
            gcp.fetch(self.client(), "not json", START, END, {"table": "my-proj-1.d.t"})


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
        self.assertEqual(hashlib.sha256(b"").hexdigest()[:4], "e3b0")


if __name__ == "__main__":
    unittest.main()


class TheEntry(StubCase):
    """fetcher.run(): the vendor, key file, hosts and params come from the
    environment Light sets; nothing else is read."""

    def test_run_from_the_environment(self):
        from finops import fetch
        d = tempfile.mkdtemp(prefix="finops-entry-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        kp = os.path.join(d, "key")
        with open(kp, "w") as f:
            f.write(KEY + "\n")
        self.stub(Anthropic.routes(Anthropic()))
        env = {"CORRAL_FETCH_VENDOR": "anthropic", "CORRAL_FETCH_KEY": kp,
               "CORRAL_FETCH_HOSTS": "127.0.0.1", "CORRAL_FETCH_PARAM_TABLE": "x"}
        doc = fetch.run(env, now=1791466622)
        self.assertEqual((doc["schema"], doc["vendor"], doc["range"]),
                         ("finops.billed/1", "anthropic",
                          {"start": "2026-09-01", "end": "2026-10-09"}))
        self.assertGreaterEqual(doc["requests"], 3)
        self.assertEqual(fetch.params_from(env), {"table": "x"})
        with self.assertRaises(FetchError):
            fetch.run(dict(env, CORRAL_FETCH_VENDOR="evil"))
        with self.assertRaises(FetchError):           # a host not granted
            fetch.run(dict(env, CORRAL_FETCH_HOSTS="api.anthropic.com"), now=1791466622)
