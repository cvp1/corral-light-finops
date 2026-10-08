"""A small HTTPS JSON client for the billing fetchers (Light plan §6.7).

Inside Light's fetch sandbox the only way out is the CONNECT proxy in
HTTPS_PROXY, which allows this grant's vendor hosts only. This client also
refuses any host outside CORRAL_FETCH_HOSTS and any plain-http URL except
to 127.0.0.1 (the test stubs). Error text carries the status and the
vendor's error type, never a header, a URL query or a key.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

MAX_BODY = 8 << 20
UA = "corral-light-finops (+https://github.com/cvp1/corral-light-finops)"


class FetchError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Client:
    def __init__(self, hosts, timeout=30, opener=None):
        self.hosts = {h.lower() for h in hosts}
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener()
        self.calls = 0

    def _check(self, url):
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        if host not in self.hosts:
            raise FetchError(f"refusing a request to {host or 'no host'}: not this "
                             f"grant's vendor")
        if u.scheme != "https" and not (u.scheme == "http" and host == "127.0.0.1"):
            raise FetchError("refusing a request that is not HTTPS")
        return host

    def json(self, method, url, headers=None, body=None, form=None):
        host = self._check(url)
        self.calls += 1
        if self.calls > 500:
            raise FetchError("more than 500 requests in one run; stopping")
        data = None
        h = {"Accept": "application/json", "User-Agent": UA}
        h.update(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            raise FetchError(_describe(host, e), status=e.code,
                             retry_after=e.headers.get("retry-after") if e.headers else None) \
                from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise FetchError(f"{host}: {type(reason).__name__}: {str(reason)[:120]}") from None
        if len(raw) > MAX_BODY:
            raise FetchError(f"{host}: the response is larger than 8 MiB")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise FetchError(f"{host}: the response is not JSON") from None


def _describe(host, e):
    what = {401: "the key was refused (401)", 403: "the key lacks permission (403)",
            404: "not found (404)", 429: "rate limited (429)"}.get(e.code, f"HTTP {e.code}")
    detail = ""
    try:
        doc = json.loads(e.read(65536).decode("utf-8", "replace"))
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict):
            detail = " ".join(str(err.get(k)) for k in ("type", "code", "status")
                              if err.get(k))
        elif isinstance(err, str):
            detail = err
    except (ValueError, OSError, AttributeError):
        pass
    finally:
        try:
            e.close()
        except Exception:  # noqa: BLE001
            pass
    return f"{host}: {what}" + (f" [{detail[:80]}]" if detail else "")
