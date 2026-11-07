#!/usr/bin/env python3
"""KVDB (Kill-It-and-Prove-It Database) - Main Entry Point.

Usage:
    python main.py <command> [args]
    python main.py --demo

Examples:
    python main.py open data.kv
    python main.py put data.kv mykey "my value"
    python main.py get data.kv mykey
    python main.py del data.kv mykey
    python main.py check data.kv
    python main.py dump data.kv
    python main.py serve data.kv
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Add src/ to path so imports resolve cleanly
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import cli
import kvdb


def run_interactive_demo() -> int:
    print("=" * 65)
    print("  KVDB (Kill-It-and-Prove-It Database) - Interactive Self-Test")
    print("=" * 65)
    test_db = "demo.kv"
    try:
        db = kvdb.Database()
        print(f"\n[1] Opening database '{test_db}'...")
        db.open(test_db)
        print(f"    -> Keys: {db.key_count}, Recovery Time: {db.recovery_ms:.2f} ms")

        print("\n[2] Performing durable PUT operations...")
        put_start = time.perf_counter()
        # Insert 50 individually fsynced durable records
        num_individual_puts = 50
        for i in range(1, num_individual_puts + 1):
            key = f"user:{i:03d}"
            val = f'{{"id": {i}, "name": "User_{i}", "role": "engineer", "status": "active"}}'
            db.put(key, val)
        put_elapsed = (time.perf_counter() - put_start) * 1000.0
        print(f"    -> Stored {num_individual_puts} durable keys individually (each fsynced to disk).")
        print(f"    -> Elapsed: {put_elapsed:.1f} ms ({put_elapsed / num_individual_puts:.2f} ms/op)")

        print("\n[3] Reading sample keys back with GET...")
        sample_keys = ["user:001", "user:025", "user:050"]
        for k in sample_keys:
            v = db.get(k)
            print(f"    -> {k} = {v.decode('utf-8', errors='replace')}")

        print("\n[4] Running integrity CHECK...")
        status = db.check()
        print(f"    -> Status: {status} across {db.key_count} keys")

        print("\n[5] Atomic BATCH operation (1,000 records atomically committed)...")
        batch_start = time.perf_counter()
        batch_ops = [
            ("put", f"sensor:{j:04d}".encode(), f'{{"temp": {20.0 + (j % 15)}, "ok": true}}'.encode())
            for j in range(1, 1001)
        ]
        # Include a delete of an earlier key to test mixed batch
        batch_ops.append(("del", b"user:050"))
        db.batch(batch_ops)
        batch_elapsed = (time.perf_counter() - batch_start) * 1000.0
        print(f"    -> Atomic batch of {len(batch_ops)} ops committed in {batch_elapsed:.1f} ms.")
        print(f"    -> Total active keys in database now: {db.key_count}")
        print(f"    -> Verified deleted key 'user:050': {db.get('user:050') is None}")
        print(f"    -> Verified sample batch key 'sensor:0500': {db.get('sensor:0500').decode()}")

        print("\n[6] Compacting log file...")
        db.compact()
        print(f"    -> Post-compaction status: {db.check()}")

        db.close()
        print("\n[7] Database cleanly closed.")
        print("=" * 65)
        print("All basic operations verified successfully!")
        print("\nQuick Commands Reference:")
        print("  - Run all unit tests:    python -m unittest discover -s tests -v")
        print("  - Run 500-kill torture:  python torture/torture.py --iterations 500 --seed 12345")
        print("  - Run 1M key benchmark:  python tests/benchmark_1m.py --keys 1000000")
        print("  - Run CLI commands:      python main.py <open|put|get|del|batch|dump|check|serve>")
        print("=" * 65)
    finally:
        for f in [test_db, f"{test_db}.lock", f"{test_db}.compacting"]:
            p = Path(f)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
    return 0


def main() -> int:
    # If called with no arguments or --demo, run interactive demo & help
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ("--demo", "demo")):
        return run_interactive_demo()
    
    # Forward all CLI subcommands to src/cli.py
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())

