"""Kill the process on either side of os.replace during compaction.

os.replace is one operating-system call. After a process kill, that call has
either not happened or it has finished. Both files involved are complete:
the old file was already fsynced, and the new file was fsynced before the call.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "cli.py"


class ReplaceKillTests(unittest.TestCase):
    def test_kill_before_replace_keeps_the_old_file(self):
        self._kill_at("before-replace")

    def test_kill_after_replace_keeps_the_new_file(self):
        self._kill_at("after-replace")

    def _kill_at(self, point: str) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "data.kv")
        env = os.environ.copy()
        env["KVDB_CRASH_AT"] = point
        proc = subprocess.Popen(
            [sys.executable, "-u", str(CLI), "serve", path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(ROOT),
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        ready = proc.stdout.readline()
        self.assertTrue(ready.startswith("READY"), ready)
        proc.stdin.write("put 1 apple red\n")
        proc.stdin.flush()
        ack = proc.stdout.readline()
        self.assertEqual(ack.strip(), "ACK 1")
        proc.stdin.write("compact 2\n")
        proc.stdin.flush()
        seen = ""
        for _ in range(10):
            line = proc.stdout.readline()
            seen += line
            if line.startswith("CRASH_AT"):
                break
        self.assertIn(f"CRASH_AT {point}", seen)
        proc.kill()
        proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()

        check = subprocess.run(
            [sys.executable, "-u", str(CLI), "get", path, "apple"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(check.stdout.strip(), "red", check.stderr)
        checked = subprocess.run(
            [sys.executable, "-u", str(CLI), "check", path],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(checked.stdout.strip(), "OK", checked.stderr)


if __name__ == "__main__":
    unittest.main()
