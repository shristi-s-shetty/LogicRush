"""Time open() after a chosen number of keys.

Usage:
    python torture/bench_recover.py --keys 1000000

Writing uses batches of 10,000 so the fsync count stays small. The number
that matters is recovery_ms on the following open. The limit in the problem
is 2,000 milliseconds at 1,000,000 keys.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kvdb


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", type=int, default=1_000_000)
    args = parser.parse_args()
    root = tempfile.mkdtemp(prefix="kvdb-bench-")
    path = os.path.join(root, "data.kv")
    db = kvdb.Database()
    db.open(path)
    started = time.perf_counter()
    for start in range(0, args.keys, kvdb.MAX_BATCH):
        count = min(kvdb.MAX_BATCH, args.keys - start)
        ops = [("put", f"k{i:07d}".encode(), b"v") for i in range(start, start + count)]
        db.batch(ops)
        print(f"wrote {start + count}", flush=True)
    write_s = time.perf_counter() - started
    db.close()
    size = os.path.getsize(path)
    db = kvdb.Database()
    db.open(path)
    count = db.key_count
    recovery_ms = db.recovery_ms
    sample = db.get(b"k0000000")
    last = db.get(f"k{args.keys - 1:07d}".encode())
    db.close()
    print(
        f"keys={count} file_mb={size / 1e6:.1f} write_s={write_s:.2f} "
        f"recovery_ms={recovery_ms:.1f}"
    )
    if count != args.keys or sample != b"v" or last != b"v":
        print("sample keys did not match", file=sys.stderr)
        return 1
    if args.keys >= 1_000_000 and recovery_ms >= 2000:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
