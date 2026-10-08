"""Corral Light's module contract, vendored for this module's tests.

Copied verbatim (whole top-level definitions) from Light's
modules.py at commit a8a8ca81576ddb350486c288b0bfad7b6ad283f5 (branch finops-phase3-plan,
2026-10-08): the manifest checks (validate_manifest) and the snapshot
validator (validate_snapshot), with the rail notice checks (plan §4.7).
Re-vendor when Light's core_api or snapshot contract changes.
"""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

CORE_API = 1

MANIFEST_SCHEMA = "corral-light.manifest/1"

SNAPSHOT_SCHEMA = "corral-light.module/1"

CORE_VERBS = frozenset((
    "pair", "key", "serve", "doctor", "worktrees", "launch", "install-service",
    "diagnose", "consult", "watch", "panes", "open", "say", "pending", "ok", "no",
    "cancel", "pause", "resume", "close", "forget", "reopen", "rename", "seat",
    "config", "attach", "quote", "later", "search", "digest", "port", "rig",
    "lanes", "update", "cli", "module", "modules", "help", "version"))

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

MANIFEST_KEYS = {"schema", "name", "title", "version", "core_api", "summary",
                 "collector", "cli", "doctor", "reads", "vendor_reports", "network",
                 "notices"}

ENTRY_KEYS = {"collector": {"script", "args", "every_s", "budget_s", "timeout_s"},
              "cli": {"script", "args"},
              "doctor": {"script", "args"}}

READS = ("claude-projects", "codex-sessions", "gemini-store", "light-feed")

VENDOR_REPORTS = ("grok-usage",)

STDOUT_CAP = 1 << 20

DEFAULT_EVERY_S, MIN_EVERY_S = 300, 60

DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 45, 300

class ModuleError(Exception):
    """A refusal, with the reason the operator sees."""

def check_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ModuleError(f"module name {name!r} must match [a-z][a-z0-9-]{{0,31}}")
    if name in CORE_VERBS:
        raise ModuleError(f"{name!r} is a corral-light verb; a module cannot take it")
    return name

def _check_script(root, rel, where):
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        raise ModuleError(f"{where}.script must be a relative path inside the module")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ModuleError(f"{where}.script {rel!r} may not contain '..', '.' or empty parts")
    if rel.startswith("-"):
        raise ModuleError(f"{where}.script may not look like an interpreter option")
    if not rel.endswith(".py"):
        raise ModuleError(f"{where}.script must be a .py file run by the core's Python")
    if root is not None:
        cur = Path(root)
        for p in parts:
            cur = cur / p
            if cur.is_symlink():
                raise ModuleError(f"{where}.script {rel!r} passes through a symlink")
        if not cur.is_file():
            raise ModuleError(f"{where}.script {rel!r} is not a regular file in the module")
    return rel

def _check_args(args, where):
    if args is None:
        return []
    if not isinstance(args, list) or len(args) > 16 or \
            not all(isinstance(a, str) and len(a) <= 200 for a in args):
        raise ModuleError(f"{where}.args must be a list of at most 16 short strings")
    return list(args)

def _int_field(entry, key, default, lo, hi, where):
    v = entry.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ModuleError(f"{where}.{key} must be an integer from {lo} to {hi}")
    return v

def validate_manifest(obj, root=None):
    """-> the manifest, normalised. Raises ModuleError with the reason.
    `root`: the module's files, to check scripts exist and are plain files."""
    if not isinstance(obj, dict):
        raise ModuleError("module.json must hold one JSON object")
    unknown = sorted(set(obj) - MANIFEST_KEYS)
    if unknown:
        raise ModuleError(f"module.json has unknown keys: {', '.join(unknown)}")
    if obj.get("schema") != MANIFEST_SCHEMA:
        raise ModuleError(f"module.json schema must be {MANIFEST_SCHEMA!r}")
    if obj.get("core_api") != CORE_API:
        raise ModuleError(f"module.json core_api {obj.get('core_api')!r} is not one this "
                          f"Corral Light speaks ({CORE_API})")
    name = check_name(obj.get("name"))
    out = {"schema": MANIFEST_SCHEMA, "name": name, "core_api": CORE_API}
    for key, cap in (("title", 60), ("version", 40), ("summary", 200)):
        v = obj.get(key, name if key == "title" else "")
        if not isinstance(v, str) or len(v) > cap:
            raise ModuleError(f"module.json {key} must be text of at most {cap} characters")
        out[key] = v
    if obj.get("network", "none") != "none":
        raise ModuleError("module.json network must be \"none\": a collector never "
                          "reaches the network")
    out["network"] = "none"
    # Rail notices (§4.7) are opt-in, and shown at install like the reads.
    if not isinstance(obj.get("notices", False), bool):
        raise ModuleError("module.json notices must be true or false")
    out["notices"] = obj.get("notices", False)
    reads = obj.get("reads", [])
    if not isinstance(reads, list) or not all(isinstance(r, str) for r in reads):
        raise ModuleError("module.json reads must be a list of names")
    bad = [r for r in reads if r not in READS]
    if bad:
        raise ModuleError(f"module.json reads {bad[0]!r} is not one of: {', '.join(READS)}")
    out["reads"] = sorted(set(reads))
    vrep = obj.get("vendor_reports", [])
    if not isinstance(vrep, list) or not all(isinstance(r, str) for r in vrep):
        raise ModuleError("module.json vendor_reports must be a list of names")
    bad = [r for r in vrep if r not in VENDOR_REPORTS]
    if bad:
        raise ModuleError(f"module.json vendor_reports {bad[0]!r} is not one of: "
                          f"{', '.join(VENDOR_REPORTS)}")
    out["vendor_reports"] = sorted(set(vrep))
    if "collector" not in obj:
        raise ModuleError("module.json needs a collector")
    for where in ("collector", "cli", "doctor"):
        if where not in obj:
            continue
        entry = obj[where]
        if not isinstance(entry, dict):
            raise ModuleError(f"module.json {where} must be an object")
        extra = sorted(set(entry) - ENTRY_KEYS[where])
        if extra:
            raise ModuleError(f"module.json {where} has unknown keys: {', '.join(extra)}")
        e = {"script": _check_script(root, entry.get("script"), where),
             "args": _check_args(entry.get("args"), where)}
        if where == "collector":
            e["every_s"] = _int_field(entry, "every_s", DEFAULT_EVERY_S, MIN_EVERY_S, 86400, where)
            e["timeout_s"] = _int_field(entry, "timeout_s", DEFAULT_TIMEOUT_S, 5,
                                        MAX_TIMEOUT_S, where)
            e["budget_s"] = _int_field(entry, "budget_s", min(30, e["timeout_s"]), 1,
                                       e["timeout_s"], where)
        out[where] = e
    return out

KINDS = ("billed", "vendor", "declared", "list", "estimate", "unknown")

LEVELS = ("ok", "info", "warn", "bad")

BLOCK_TYPES = ("tiles", "meter", "table", "note", "link")

MAX_BLOCKS, MAX_TILES, MAX_ROWS, MAX_COLS = 50, 24, 200, 12

LABEL_CAP, CELL_CAP = 200, 500

def _text(v, cap, dropped=None):
    if v is None:
        return ""
    if isinstance(v, bool):
        v = "yes" if v else "no"
    if isinstance(v, (int, float)):
        v = str(v) if not isinstance(v, float) or math.isfinite(v) else ""
    if not isinstance(v, str):
        return ""
    v = v.replace("\x00", "")
    if len(v) > cap:
        if dropped is not None:
            dropped["chars"] = dropped.get("chars", 0) + (len(v) - cap)
        v = v[:cap - 1] + "…"
    return v

def safe_https_url(url):
    if not isinstance(url, str) or len(url) > 2000:
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.scheme != "https" or not u.hostname or u.username or u.password or \
            any(c in url for c in "\r\n\t \\"):
        return None
    return url

def _validate_block(b):
    if not isinstance(b, dict):
        return {"type": "unsupported", "was": type(b).__name__}
    t = b.get("type")
    if t not in BLOCK_TYPES:
        return {"type": "unsupported", "was": _text(t, 40)}
    d = {}
    if t == "tiles":
        items = b.get("items") if isinstance(b.get("items"), list) else []
        out = []
        for it in items[:MAX_TILES]:
            if not isinstance(it, dict):
                continue
            out.append({"label": _text(it.get("label"), LABEL_CAP, d),
                        "value": _text(it.get("value"), LABEL_CAP, d),
                        "kind": it.get("kind") if it.get("kind") in KINDS else "unknown",
                        "level": it.get("level") if it.get("level") in LEVELS else "info",
                        "note": _text(it.get("note"), CELL_CAP, d),
                        "fresh_at": _text(it.get("fresh_at"), 40, d)})
        blk = {"type": "tiles", "items": out}
        if len(items) > MAX_TILES:
            d["items"] = len(items) - MAX_TILES
    elif t == "meter":
        pct = b.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
            pct = 0
        blk = {"type": "meter", "label": _text(b.get("label"), LABEL_CAP, d),
               "pct": max(0, min(100, pct)),
               "kind": b.get("kind") if b.get("kind") in KINDS else "unknown",
               "level": b.get("level") if b.get("level") in LEVELS else "info",
               "note": _text(b.get("note"), CELL_CAP, d)}
    elif t == "table":
        cols = b.get("columns") if isinstance(b.get("columns"), list) else []
        rows = b.get("rows") if isinstance(b.get("rows"), list) else []
        ncol = min(len(cols), MAX_COLS)
        out_rows = []
        for r in rows[:MAX_ROWS]:
            if not isinstance(r, list):
                continue
            cells = [_text(c, CELL_CAP, d) for c in r[:MAX_COLS]]
            out_rows.append(cells)
        blk = {"type": "table", "title": _text(b.get("title"), LABEL_CAP, d),
               "columns": [_text(c, LABEL_CAP, d) for c in cols[:ncol]], "rows": out_rows}
        if len(rows) > MAX_ROWS:
            d["rows"] = len(rows) - MAX_ROWS
        if len(cols) > MAX_COLS:
            d["columns"] = len(cols) - MAX_COLS
    elif t == "note":
        blk = {"type": "note", "text": _text(b.get("text"), CELL_CAP, d),
               "level": b.get("level") if b.get("level") in LEVELS else "info"}
    else:  # link
        url = safe_https_url(b.get("url"))
        blk = {"type": "link", "label": _text(b.get("label"), LABEL_CAP, d),
               "url": url if url else _text(b.get("url"), LABEL_CAP, d),
               "safe": bool(url)}
    if d:
        blk["dropped"] = d
    return blk

NOTICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

NOTICE_LEVELS = ("info", "warn", "bad")

NOTICE_TITLE_CAP, NOTICE_TEXT_CAP = 80, 300

MAX_NOTICES = 5                  # kept per snapshot

_LEVEL_RANK = {"bad": 0, "warn": 1, "info": 2}

def _parse_iso(v):
    """ISO time -> epoch seconds, or None. A time with no zone is UTC."""
    if not isinstance(v, str) or not v or len(v) > 40:
        return None
    try:
        t = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    try:
        ts = t.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return ts if math.isfinite(ts) else None

def _validate_notices(raw):
    """A snapshot's `notices` (§4.7) -> (kept, dropped count)."""
    if not isinstance(raw, list):
        return [], 0
    seen, out, dropped = set(), [], 0
    for n in raw[:200]:
        nid = n.get("id") if isinstance(n, dict) else None
        title = _text(n.get("title"), NOTICE_TITLE_CAP) if isinstance(n, dict) else ""
        if not isinstance(nid, str) or not NOTICE_ID_RE.match(nid) or nid in seen \
                or not title.strip():
            dropped += 1
            continue
        seen.add(nid)
        exp = _parse_iso(n.get("expires_at"))
        out.append({"id": nid,
                    "level": n.get("level") if n.get("level") in NOTICE_LEVELS else "info",
                    "title": title, "text": _text(n.get("text"), NOTICE_TEXT_CAP),
                    "expires_at": (datetime.fromtimestamp(exp, timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%SZ")
                                   if exp is not None else None)})
    dropped += max(0, len(raw) - 200)
    out.sort(key=lambda x: (_LEVEL_RANK[x["level"]], x["id"]))
    dropped += max(0, len(out) - MAX_NOTICES)
    return out[:MAX_NOTICES], dropped

def validate_snapshot(raw, notices=False):
    """bytes or str -> (snapshot, None) or (None, error). Every string is
    text, every enum is mapped, every bound enforced (§4.5). `notices`: the
    verified manifest opted in (§4.7); otherwise the field is ignored."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > STDOUT_CAP:
            return None, "the snapshot is larger than 1 MiB"
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "the snapshot is not UTF-8"
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "the collector's output is not JSON"
    if not isinstance(obj, dict):
        return None, "the snapshot is not a JSON object"
    if obj.get("schema") != SNAPSHOT_SCHEMA:
        return None, f"the snapshot's schema is not {SNAPSHOT_SCHEMA!r}"
    view = obj.get("view") if isinstance(obj.get("view"), list) else []
    snap = {"schema": SNAPSHOT_SCHEMA, "ok": obj.get("ok") is True,
            "generated_at": _text(obj.get("generated_at"), 40),
            "error": _text(obj.get("error"), CELL_CAP) or None,
            "progress": None,
            "view": [_validate_block(b) for b in view[:MAX_BLOCKS]]}
    pr = obj.get("progress")
    if isinstance(pr, dict):
        pct = pr.get("done_pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
            pct = None
        snap["progress"] = {"phase": _text(pr.get("phase"), 40),
                            "done_pct": None if pct is None else max(0, min(100, pct)),
                            "note": _text(pr.get("note"), LABEL_CAP)}
    if len(view) > MAX_BLOCKS:
        snap["truncated"] = {"blocks": len(view) - MAX_BLOCKS}
    if notices:
        snap["notices"], nd = _validate_notices(obj.get("notices"))
        if nd:
            snap["notices_dropped"] = nd
    return snap, None
