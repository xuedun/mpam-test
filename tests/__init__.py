"""Offline unit tests: no root, no KVM, no resctrl, no VM launches.

Everything here exercises pure logic -- protocol parsing, schemata handling,
placement arithmetic, allocation strategies, analysis -- so the experiment's
correctness-critical decisions are covered without a host. Run with:

    python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
