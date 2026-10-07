"""API list prices (data/prices.toml): effective-dated rows per raw model
id, integer micro-dollars per million tokens, one source URL per row.

Normalisation is a lookup, never a rewrite: an `[[alias]]` row says which
price row an id is billed as, and the raw id is kept everywhere else. A
row with `over_input` is a long-context tier that applies when one
request's input passes that count. A model with no row is "unpriced"; its
tokens are counted apart and never priced at $0.
"""
import os
from datetime import datetime, timezone

from finops import tomlmini

DIMENSIONS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(HERE, "data", "prices.toml")


class PriceError(ValueError):
    pass


def _day_ts(s, where):
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        raise PriceError(f"{where}: dates are YYYY-MM-DD") from None


class Prices:
    def __init__(self, rows, aliases):
        self.rows = rows            # model -> [row sorted by from_ts]
        self.aliases = aliases

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        with open(path, encoding="utf-8") as f:
            doc = tomlmini.loads_strict(f.read())
        rows, aliases = {}, {}
        for i, r in enumerate(doc.get("price", []), 1):
            where = f"price #{i}"
            model = r.get("model")
            if not isinstance(model, str) or not model:
                raise PriceError(f"{where}: model is required")
            if not isinstance(r.get("source"), str) or not r["source"].startswith("https://"):
                raise PriceError(f"{where} ({model}): every row needs an https source")
            row = {"model": model, "source": r["source"], "checked": r.get("checked"),
                   "from_ts": _day_ts(r["from"], where) if "from" in r else float("-inf"),
                   "from": r.get("from"),
                   "over_input": r.get("over_input")}
            for d in DIMENSIONS:
                v = r.get(d)
                if v is not None and (not isinstance(v, int) or v < 0):
                    raise PriceError(f"{where} ({model}): {d} must be a whole number of "
                                     f"micro-dollars per million tokens")
                row[d] = v
            if row["input"] is None or row["output"] is None:
                raise PriceError(f"{where} ({model}): input and output are required")
            extra = set(r) - set(DIMENSIONS) - {"model", "source", "checked", "from",
                                                "over_input", "note"}
            if extra:
                raise PriceError(f"{where} ({model}): unknown keys {sorted(extra)}")
            rows.setdefault(model, []).append(row)
        for a in doc.get("alias", []):
            if not isinstance(a.get("model"), str) or not isinstance(a.get("price_as"), str):
                raise PriceError("alias rows need model and price_as")
            aliases[a["model"]] = a["price_as"]
        for model in rows:
            rows[model].sort(key=lambda x: (x["from_ts"], x["over_input"] or 0))
        return cls(rows, aliases)

    def row(self, model, ts, input_tokens=0):
        """The row in force for `model` at `ts` for a request with this much
        input, or None (unpriced)."""
        if not model:
            return None
        key = model if model in self.rows else self.aliases.get(model)
        cands = self.rows.get(key) if key else None
        if not cands:
            return None
        live = [r for r in cands if r["from_ts"] <= ts]
        if not live:
            return None
        start = max(r["from_ts"] for r in live)
        live = [r for r in live if r["from_ts"] == start]
        best = None
        for r in live:
            over = r["over_input"]
            if over is None:
                if best is None:
                    best = r
            elif input_tokens > over and (best is None or (best["over_input"] or 0) < over):
                best = r
        return best

    def cost(self, model, ts, parts, input_tokens=None):
        """parts: {dimension: tokens}. -> integer micro-dollars, or None when
        the model is unpriced. A dimension with no price in the row falls
        back to the row's input price (cache dims) and is never free."""
        total_in = input_tokens if input_tokens is not None else sum(
            parts.get(d, 0) for d in ("input", "cache_read", "cache_write_5m", "cache_write_1h"))
        r = self.row(model, ts, total_in)
        if r is None:
            return None
        micros = 0
        for d, n in parts.items():
            if not n:
                continue
            rate = r.get(d)
            if rate is None:
                rate = r["input"] if d != "output" else r["output"]
            micros += n * rate
        return (micros + 500000) // 1000000
