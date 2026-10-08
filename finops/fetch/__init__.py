"""Billing fetchers (Light plan §6.7): one vendor per run, one key, the
vendor's exact hosts only. `run()` returns the result document, or raises
with a message that carries no key."""
import os
import time

from finops.fetch import anthropic, common, gcp, openai, xai
from finops.fetch.http import Client, FetchError

VENDORS = {"anthropic": anthropic, "openai": openai, "xai": xai, "gcp": gcp}


def params_from(environ):
    p = "CORRAL_FETCH_PARAM_"
    return {k[len(p):].lower(): v for k, v in environ.items() if k.startswith(p)}


def run(environ=None, now=None, client=None):
    e = os.environ if environ is None else environ
    vendor = e.get("CORRAL_FETCH_VENDOR", "")
    mod = VENDORS.get(vendor)
    if mod is None:
        raise FetchError(f"unknown vendor {vendor!r}")
    with open(e["CORRAL_FETCH_KEY"], encoding="utf-8") as f:
        key = f.read().strip()
    hosts = [h for h in e.get("CORRAL_FETCH_HOSTS", "").split(",") if h]
    client = client or Client(hosts)
    start, end = common.window(time.time() if now is None else now)
    doc = mod.fetch(client, key, start, end, params_from(e))
    doc["requests"] = client.calls
    return doc
