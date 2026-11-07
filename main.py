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
        db.put("team", "LogicRush")
        db.put("challenge", "Kill It and Prove It")
        db.put("durability", "fsync-guaranteed")
        print("    -> Stored 3 keys successfully.")

        print("\n[3] Reading keys back with GET...")
        for k in ["team", "challenge", "durability"]:
            v = db.get(k)
            print(f"    -> {k} = {v.decode('utf-8', errors='replace')}")

        print("\n[4] Running integrity CHECK...")
        status = db.check()
        print(f"    -> Status: {status}")

        print("\n[5] Atomic BATCH operation (1 put, 1 delete)...")
        db.batch([("put", b"temp_key", b"temp_val"), ("del", b"durability")])
        print(f"    -> Active keys now: {db.key_count}")
        print(f"    -> temp_key = {db.get('temp_key').decode()}")
        print(f"    -> durability deleted: {db.get('durability') is None}")

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
