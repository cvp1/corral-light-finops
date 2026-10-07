"""FinOps collector: one budgeted scan, then one snapshot on stdout.

Run by Light as `python -I -B collector.py snapshot --budget N` inside the
module sandbox. Isolated mode leaves the script's folder off sys.path, so
it is added here. A failure exits non-zero, so Light keeps the last good
snapshot and shows the error.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from finops.main import collector_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(collector_main(sys.argv[1:]))
