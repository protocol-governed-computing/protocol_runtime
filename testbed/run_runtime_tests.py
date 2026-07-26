#!/usr/bin/env python3
"""
Runtime Tests runner — runtime testbed.

Test category: Runtime Tests
Purpose: verify execution substrate correctness — snapshot loading, CS binding,
workflow execution, determinism, and failure modes.

Uses standard Python unittest framework (no external dependencies).
"""

import sys
import unittest
import os
from pathlib import Path


def _inject_workspace_env() -> None:
    """
    Auto-populate PGS_WORKSPACE and PGS_DATA_ROOT from the standard sibling
    layout if the caller has not set them. This allows integration tests to run
    without manual env-var setup when pgs_workspace is a sibling of runtime.

    Layout assumed:
        <base>/runtime/   ← this repo
        <base>/pgs_workspace/ ← compiled workspace (sibling)
    """
    if os.environ.get("PGS_WORKSPACE"):
        return  # already set by caller — respect it

    runtime_root = Path(__file__).resolve().parent.parent  # runtime/
    workspace = runtime_root.parent / "pgs_workspace"

    if not workspace.is_dir():
        return  # sibling workspace not found — tests will skip as before

    os.environ["PGS_WORKSPACE"] = str(workspace)

    if not os.environ.get("PGS_DATA_ROOT"):
        os.environ["PGS_DATA_ROOT"] = str(workspace / "data")


def run_tests(verbosity=2):
    """Run the PGC runtime tests.

    Default suite = PGC warm-boot (testbed.pgc): boots the assembled snapshot through its manifest.
    Legacy RI-0 PGS-workspace execution tests (testbed.implementations.tests) require a PGS
    workspace + a domain workflow the platform snapshot lacks; run only with PGC_RI0_LEGACY=1.
    """
    loader = unittest.TestLoader()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    top_level_dir = os.path.dirname(script_dir)  # repo root (protocol_runtime/)

    suite = unittest.TestSuite()

    # --- canonical PGC suite ---
    suite.addTests(loader.discover("testbed.pgc", pattern="test_*.py", top_level_dir=top_level_dir))

    # --- legacy RI-0 execution suite (opt-in) ---
    if os.environ.get("PGC_RI0_LEGACY"):
        _inject_workspace_env()
        suite.addTests(
            loader.discover("testbed.implementations.tests", pattern="test_*.py",
                            top_level_dir=top_level_dir)
        )

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    # Check for verbose flag
    verbosity = 2 if '-v' in sys.argv or '--verbose' in sys.argv else 1

    sys.exit(run_tests(verbosity))
