"""A small JSON client for the billing fetchers (Light plan §6.7.2).

FinOps holds no billing key. Inside Light's fetch sandbox the only way out
is Light's fetch proxy at CORRAL_FETCH_API: this client sends it plain HTTP
in proxy form (`GET https://api.anthropic.com/... HTTP/1.1`), and the proxy
checks the host, adds the vendor's credential, makes the HTTPS request and
returns the answer. The client also refuses any host outside
CORRAL_FETCH_HOSTS and any URL that is not https. Error text carries the
status and the vendor's error type, never a header or a URL query.
"""
import http.client
import json
import urllib.parse
from decimal import Decimal

MAX_BODY = 8 << 20
UA = "corral-light-finops (+https://github.com/cvp1/corral-light-finops)"


class FetchError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Client:
    """`api`: the proxy's http://host:port, from CORRAL_FETCH_API."""

    def __init__(self, hosts, api, timeout=30):
        self.hosts = {h.lower() for h in hosts}
        u = urllib.parse.urlsplit(api or "")
        if u.scheme != "http" or not u.hostname or not u.port:
            raise FetchError("no fetch proxy (CORRAL_FETCH_API): this Light is older than "
                             "the one FinOps billing needs; update Light")
        self.proxy = (u.hostname, u.port)
        self.timeout = timeout
        self.calls = 0

    def _check(self, url):
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        if host not in self.hosts:
            raise FetchError(f"refusing a request to {host or 'no host'}: not this "
                             f"grant's vendor")
        if u.scheme != "https":
            raise FetchError("refusing a request that is not HTTPS")
        return host

    def json(self, method, url, headers=None, body=None):
        host = self._check(url)
        self.calls += 1
        if self.calls > 500:
            raise FetchError("more than 500 requests in one run; stopping")
        h = {"Accept": "application/json", "User-Agent": UA}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, default=str).encode()
            h["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(*self.proxy, timeout=self.timeout)
        try:
            conn.request(method, url, body=data, headers=h)
            r = conn.getresponse()
            raw = r.read(MAX_BODY + 1)
            status, retry = r.status, r.getheader("retry-after")
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"{host}: {type(e).__name__}: {str(e)[:120]}") from None
        finally:
            conn.close()
        if len(raw) > MAX_BODY:
            raise FetchError(f"{host}: the response is larger than 8 MiB")
        if status >= 400:
            raise FetchError(_describe(host, status, raw), status=status, retry_after=retry)
        try:
            # Decimal, not float: an amount must not lose a cent on the way.
            return json.loads(raw.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, ValueError):
            raise FetchError(f"{host}: the response is not JSON") from None


def _describe(host, status, raw):
    what = {401: "the key was refused (401)", 403: "refused (403)",
            404: "not found (404)", 429: "rate limited (429)",
            502: "the proxy could not complete the request (502)"}.get(status,
                                                                       f"HTTP {status}")
    detail = ""
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict):
            detail = " ".join(str(err.get(k)) for k in ("type", "code", "status")
                              if err.get(k))
        elif isinstance(err, str):
            detail = err
    except ValueError:
        detail = raw.decode("utf-8", "replace").strip().splitlines()[0] if raw.strip() else ""
    return f"{host}: {what}" + (f" [{detail[:80]}]" if detail else "")
