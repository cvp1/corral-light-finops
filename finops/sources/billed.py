"""Billing API results (Light plan §6.7), as the core stored them: one
file per granted key in CORRAL_MODULE_FETCHED, read-only here.

Each file is a complete fetch (the fetcher stores nothing otherwise). A
newer fetch replaces that account's days inside its own range, and only
those: a day outside the range keeps its last complete figure. Amounts stay
exact decimal text in the vendor's currency; they are never priced,
converted or added to a subscription.
"""
import json
import os
import re
from decimal import Decimal, InvalidOperation

WRAP_SCHEMA = "corral-light.fetch-result/1"
RESULT_SCHEMA = "finops.billed/1"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CUR_RE = re.compile(r"^[A-Z]{3}$")
AMOUNT_RE = re.compile(r"^-?\d{1,15}(\.\d{1,15})?$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
VENDORS = ("anthropic", "openai", "xai", "gcp")
MAX_DAYS = 400


def _text(v, cap=80):
    return v[:cap] if isinstance(v, str) else ""


def parse(doc):
    """A stored fetch -> (account, vendor, org, fetched_at, start, end,
    [(day, currency, amount)], notes), or raise ValueError."""
    if not isinstance(doc, dict) or doc.get("schema") != WRAP_SCHEMA:
        raise ValueError("not a fetch result")
    key, vendor, at = doc.get("key"), doc.get("vendor"), doc.get("fetched_at")
    if not isinstance(key, str) or not KEY_RE.match(key) or vendor not in VENDORS or \
            not isinstance(at, str) or not at:
        raise ValueError("a fetch result without key, vendor or time")
    r = doc.get("result")
    if not isinstance(r, dict) or r.get("schema") != RESULT_SCHEMA or r.get("vendor") != vendor:
        raise ValueError("a fetch result in an unknown shape")
    rng = r.get("range") or {}
    start, end = rng.get("start"), rng.get("end")
    if not (isinstance(start, str) and DAY_RE.match(start) and isinstance(end, str)
            and DAY_RE.match(end) and start < end):
        raise ValueError("a fetch result without a valid range")
    days = r.get("days")
    if not isinstance(days, dict) or len(days) > MAX_DAYS:
        raise ValueError("a fetch result without valid days")
    rows = []
    for day, per in days.items():
        if not DAY_RE.match(str(day)) or not (start <= day < end) or not isinstance(per, dict):
            raise ValueError("a fetch result with a day outside its range")
        for cur, amt in per.items():
            if not CUR_RE.match(str(cur)) or not isinstance(amt, str) or \
                    not AMOUNT_RE.match(amt):
                raise ValueError("a fetch result with an unreadable amount")
            try:
                Decimal(amt)
            except InvalidOperation:
                raise ValueError("a fetch result with an unreadable amount") from None
            rows.append((day, cur, amt))
    org = r.get("org") if isinstance(r.get("org"), dict) else {}
    notes = [_text(n, 200) for n in (r.get("notes") or [])[:5] if isinstance(n, str)]
    return ("api:" + key, vendor, {"id": _text(org.get("id")), "name": _text(org.get("name"))},
            at, start, end, rows, notes)


def ingest(ledger, fetched_dir, notes=None):
    """Read every stored fetch newer than the ledger's; -> accounts updated."""
    if not fetched_dir or not os.path.isdir(fetched_dir):
        return 0
    n = 0
    for fn in sorted(os.listdir(fetched_dir)):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        path = os.path.join(fetched_dir, fn)
        try:
            if os.path.getsize(path) > (2 << 20):
                raise ValueError("larger than 2 MiB")
            with open(path, encoding="utf-8") as f:
                acct, vendor, org, at, start, end, rows, nts = parse(json.load(f))
        except (OSError, ValueError) as e:
            if notes is not None:
                notes.append(f"A billing API result could not be read ({fn}): {str(e)[:120]}")
            continue
        old = ledger.one("SELECT fetched_at FROM billed_fetch WHERE account=?", (acct,))
        if old is not None and old >= at:
            continue
        had = ledger.one("SELECT COUNT(*) FROM billed_day WHERE account=? AND day>=? AND day<?",
                         (acct, start, end)) or 0
        keep = not rows and had
        if keep and notes is not None:
            notes.append(f"A {vendor} billing fetch returned no days; the earlier figures "
                         f"for that range are kept.")
        ledger.begin()
        try:
            if not keep:
                ledger.db.execute("DELETE FROM billed_day WHERE account=? AND day>=? AND day<?",
                                  (acct, start, end))
            ledger.db.executemany("INSERT INTO billed_day VALUES (?,?,?,?)",
                                  [(acct, d, c, a) for d, c, a in rows])
            ledger.db.execute(
                "INSERT INTO billed_fetch VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account) DO "
                "UPDATE SET vendor=excluded.vendor, org_id=excluded.org_id, "
                "org_name=excluded.org_name, fetched_at=excluded.fetched_at, "
                "range_start=excluded.range_start, range_end=excluded.range_end, "
                "notes=excluded.notes",
                (acct, vendor, org["id"], org["name"], at, start, end, json.dumps(nts)))
            ledger.commit()
        except BaseException:
            ledger.rollback()
            raise
        n += 1
    return n


def accounts(ledger):
    """-> [{account, vendor, org_id, org_name, fetched_at, notes}]"""
    out = []
    for acct, vendor, oid, oname, at, notes in ledger.q(
            "SELECT account, vendor, org_id, org_name, fetched_at, notes FROM billed_fetch "
            "ORDER BY account"):
        try:
            nl = json.loads(notes or "[]")
        except ValueError:
            nl = []
        out.append({"account": acct, "vendor": vendor, "org_id": oid or "",
                    "org_name": oname or "", "fetched_at": at, "notes": nl})
    return out


def totals(ledger, account, first_day, end_day):
    """{currency: Decimal} for days in [first_day, end_day)."""
    out = {}
    for cur, amt in ledger.q("SELECT currency, amount FROM billed_day WHERE account=? AND "
                             "day>=? AND day<?", (account, first_day, end_day)):
        out[cur] = out.get(cur, Decimal(0)) + Decimal(amt)
    return out
