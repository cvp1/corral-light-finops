"""FinOps fetcher: one vendor billing API, one granted key, one result.

Run by Light as `python -I -B fetcher.py` in its fetch sandbox, behind a
proxy that allows the grant's vendor hosts only. The result is one JSON
document on stdout; any failure exits non-zero with one short line and
nothing is stored, so a partial fetch never replaces a complete one.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from finops.fetch import FetchError, run  # noqa: E402


def main():
    try:
        doc = run()
    except FetchError as e:
        print(f"finops fetch: {e}", file=sys.stderr, flush=True)
        return 2 if e.status == 429 else 1
    except (ValueError, KeyError, TypeError, OSError) as e:
        print(f"finops fetch: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr, flush=True)
        return 1
    sys.stdout.write(json.dumps(doc, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
