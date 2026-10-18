#!/usr/bin/env python3
"""
Runtime Tests runner — runtime testbed.

Runs the PGC runtime tests (testbed.pgc): each boots the assembled snapshot through its manifest.
Uses standard Python unittest framework (no external dependencies).
"""

import os
import sys
import unittest


def run_tests(verbosity=2):
    """Run the PGC runtime tests."""
    loader = unittest.TestLoader()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    top_level_dir = os.path.dirname(script_dir)  # repo root (protocol_runtime/)
    suite = loader.discover("testbed.pgc", pattern="test_*.py", top_level_dir=top_level_dir)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    verbosity = 2 if '-v' in sys.argv or '--verbose' in sys.argv else 1
    sys.exit(run_tests(verbosity))
