# Vendored from Corral Light corral_core/tomlmini.py (last changed in 738724a; MIT, same author).
# Keep in step with Light: the config must stay readable by its reader.
#!/usr/bin/python3
"""TOML read/write: `tomllib` on 3.11+, else a strict reader for the subset
Corral's files use (comments, `key = "string"|int`, `[[name]]` headers) that
refuses anything else. `basic()` emits an escaped TOML basic string."""
import re

_KEY = r"[A-Za-z0-9_-]+"
_ASSIGN = re.compile(r"^(" + _KEY + r")\s*=\s*(.*)$")
_AOT = re.compile(r"^\[\[\s*(" + _KEY + r")\s*\]\]\s*(#.*)?$")
_INT = re.compile(r"^([+-]?(?:0|[1-9](?:_?[0-9])*))\s*(#.*)?$")
_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r",
        '"': '"', "\\": "\\"}


def loads(text):
    """Parse `text` with tomllib if present, else the strict reader; ValueError on failure."""
    try:
        import tomllib                                  # 3.11+
    except ImportError:
        return loads_strict(text)
    return tomllib.loads(text)


def _basic(s, n):
    """One basic string starting at s[0] == '"' -> (value, rest)."""
    out, i = [], 1
    while i < len(s):
        ch = s[i]
        if ch == '"':
            return "".join(out), s[i + 1:]
        if ch == "\\":
            e = s[i + 1:i + 2]
            if e in _ESC:
                out.append(_ESC[e])
                i += 2
                continue
            width = {"u": 4, "U": 8}.get(e)
            hexd = s[i + 2:i + 2 + width] if width else ""
            if not width or not re.fullmatch(r"[0-9A-Fa-f]{%d}" % width, hexd):
                raise ValueError(f"line {n}: invalid escape in a string")
            cp = int(hexd, 16)
            if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                raise ValueError(f"line {n}: invalid unicode escape")
            out.append(chr(cp))
            i += 2 + width
            continue
        if (ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F:
            raise ValueError(f"line {n}: a control character in a string "
                             f"must be escaped")
        out.append(ch)
        i += 1
    raise ValueError(f"line {n}: unterminated string")


def _value(raw, n):
    if raw.startswith('"'):
        if raw.startswith('"""'):
            raise ValueError(f"line {n}: multi-line strings are not read here")
        val, rest = _basic(raw, n)
        rest = rest.strip()
        if rest and not rest.startswith("#"):
            raise ValueError(f"line {n}: unexpected text after the string")
        return val
    m = _INT.match(raw)
    if m:
        return int(m.group(1).replace("_", ""))
    raise ValueError(f"line {n}: value must be a quoted string or an integer")


def loads_strict(text):
    """The strict subset reader, used regardless of Python version."""
    root, table = {}, None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            m = _AOT.match(line)
            if not m:
                raise ValueError(f"line {n}: only `[[name]]` table headers are "
                                 f"read here")
            name = m.group(1)
            arr = root.setdefault(name, [])
            if not isinstance(arr, list):
                raise ValueError(f"line {n}: {name!r} is already a key")
            table = {}
            arr.append(table)
            continue
        m = _ASSIGN.match(line)
        if not m:
            raise ValueError(f"line {n}: only `key = value` lines are read here")
        key, val = m.group(1), _value(m.group(2), n)
        into = root if table is None else table
        if key in into:
            raise ValueError(f"line {n}: duplicate key {key!r}")
        into[key] = val
    return root


def basic(value):
    """One TOML basic string with all control characters escaped, so no value
    can start a new key."""
    simple = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t",
              "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    out = ['"']
    for ch in str(value):
        if ch in simple:
            out.append(simple[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)
