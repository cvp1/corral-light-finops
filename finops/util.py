"""Small shared helpers: the module's environment, time, money, files."""
import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:                                   # pragma: no cover (3.8)
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

TICKS_PER_USD = 10 ** 10          # Grok's costUsdTicks
MICROS_PER_USD = 10 ** 6


class Env:
    """What Light hands the module (docs/finops-module-plan.md §5.2). Tests
    build one directly; the entry points use from_environ()."""

    def __init__(self, data, config=None, feed=None, reads=None, sandboxed=True,
                 tz=None, now=None):
        self.data = data
        self.config = config
        self.feed = feed or None
        self.reads = reads or {}
        self.sandboxed = sandboxed
        self.tz = tz
        self._now = now

    @classmethod
    def from_environ(cls, environ=None):
        e = os.environ if environ is None else environ
        reads = {}
        for k, v in e.items():
            if k.startswith("CORRAL_READ_"):
                name = k[len("CORRAL_READ_"):].lower().replace("_", "-")
                reads[name] = [p for p in v.split(os.pathsep) if p]
        data = e.get("CORRAL_MODULE_DATA")
        if not data:
            raise SystemExit("finops: CORRAL_MODULE_DATA is not set; this runs under "
                             "corral-light (corral-light module run finops)")
        return cls(data=data, config=e.get("CORRAL_MODULE_CONFIG") or None,
                   feed=e.get("CORRAL_MODULE_FEED") or None, reads=reads,
                   sandboxed=e.get("CORRAL_MODULE_SANDBOXED", "1") == "1",
                   tz=e.get("TZ") or None)

    def now(self):
        return self._now if self._now is not None else time.time()


# ── time ────────────────────────────────────────────────────────────────────

def zone(name):
    """A tzinfo for an IANA name; UTC when the name is unknown or empty."""
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return timezone.utc


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
                  r"(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$")


def iso_ns(v):
    """An ISO time (fraction up to nanoseconds; no zone means UTC) or an
    integer of epoch ns/ms/s -> integer epoch nanoseconds, or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        if v > 10 ** 17:
            return v                               # ns
        if v > 10 ** 11:
            return v * 10 ** 6                     # ms
        return v * 10 ** 9 if v > 0 else None      # s
    if not isinstance(v, str):
        return None
    m = _ISO.match(v.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tzs = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc)
    except ValueError:
        return None
    if tzs and tzs != "Z":
        sign = 1 if tzs[0] == "+" else -1
        hh, mm = int(tzs[1:3]), int(tzs[-2:])
        dt -= sign * timedelta(hours=hh, minutes=mm)
    ns = int((dt - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()) * 10 ** 9
    if frac:
        ns += int(frac.ljust(9, "0"))
    return ns


def iso_s(v):
    ns = iso_ns(v)
    return None if ns is None else ns / 1e9


def utc_iso(s):
    return datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_start(now_s, tz):
    """Epoch seconds of the first instant of the month containing now, in tz."""
    local = datetime.fromtimestamp(now_s, tz)
    first = datetime(local.year, local.month, 1, tzinfo=tz)
    return first.timestamp()


def month_label(now_s, tz):
    return datetime.fromtimestamp(now_s, tz).strftime("%B %Y")


def day_of(ts_s, tz):
    return datetime.fromtimestamp(ts_s, tz).strftime("%Y-%m-%d")


def short_when(ts_s, now_s, tz):
    """'in 3 h 10 m' for soon, else a local weekday and time."""
    delta = ts_s - now_s
    if 0 <= delta < 86400:
        h, m = int(delta // 3600), int(delta % 3600 // 60)
        return f"in {h} h {m} m" if h else f"in {m} m"
    return datetime.fromtimestamp(ts_s, tz).strftime("%a %d %b %H:%M")


def age_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} m"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


# ── money and counts ───────────────────────────────────────────────────────

def usd(micros):
    """Integer micro-dollars -> '$1,234.56'; under a cent shows '<$0.01'."""
    if micros is None:
        return "unreported"
    if 0 < micros < 5000:
        return "<$0.01"
    cents = (micros + 5000) // 10000
    return "${:,}.{:02d}".format(cents // 100, cents % 100)


def usd_cents(cents):
    if cents is None:
        return "unreported"
    return "${:,}".format(cents // 100) if cents % 100 == 0 else \
        "${:,}.{:02d}".format(cents // 100, cents % 100)


def tokens(n):
    if n is None:
        return "unreported"
    if n >= 10 ** 9:
        return f"{n / 1e9:.1f} B"
    if n >= 10 ** 6:
        return f"{n / 1e6:.1f} M"
    if n >= 10 ** 4:
        return f"{n / 1e3:.0f} k"
    return f"{n:,}"


def ticks_to_micros(ticks):
    return ticks // (TICKS_PER_USD // MICROS_PER_USD)


# ── files ──────────────────────────────────────────────────────────────────

def atomic_write(path, text, mode=0o600):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def salt(data_dir):
    """32 random bytes in the data dir, created once. Hashes of vendor
    account ids use it, so the ledger never holds a raw id."""
    p = os.path.join(data_dir, "salt")
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    with open(p, "rb") as f:
        return f.read()


def account_hash(data_dir, lane, raw_id):
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    h = hashlib.sha256(salt(data_dir) + lane.encode() + b"\0" + raw_id.strip().encode())
    return h.hexdigest()
