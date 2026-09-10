"""
cli.py — Standalone entry point for the trace examiner.

Usage:
    python -m runtime.examine.cli <trace_file.jsonl>

Exits 0 on clean execution or business violation.
Exits 1 on structural failure (bug in protocol wiring, CT, or binding).
"""

import sys
from pathlib import Path

from runtime.examine import analyze, TraceParseError


def main() -> None:
    if len(sys.argv) != 2:
        print(
            "Usage: python -m runtime.examine.cli <trace_file.jsonl>",
            file=sys.stderr,
        )
        sys.exit(2)

    trace_path = Path(sys.argv[1])

    report = analyze(trace_path)
    print(report.format())

    if report.has_structural_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
