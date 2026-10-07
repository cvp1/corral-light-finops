"""`corral-light finops ...`: setup, doctor, show, accounts.

Run by Light inside the module sandbox with the terminal passed through
and the config folder writable (only here; the collector reads it)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from finops.main import cli_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
