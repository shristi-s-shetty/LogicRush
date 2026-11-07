#!/usr/bin/env python3
"""1M Key Recovery Benchmark wrapper for tests/ folder."""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "torture"))

import bench_recover

if __name__ == "__main__":
    sys.exit(bench_recover.main())

