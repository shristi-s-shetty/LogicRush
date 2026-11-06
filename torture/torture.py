"""Kill the database on purpose and check the promise.

This tool is the referee. It starts src/cli.py serve as a child process, lets two
writer threads send operations, and kills that child with subprocess.Popen.kill().
On Windows that is TerminateProcess: no cleanup code runs. The kill time is a
random number of milliseconds from the seed, not "after N operations".

An operation is added to the oracle only when this process reads "ACK <id>"
from the child's stdout. Then we open, check, and dump. The dump must equal the
ACK history, or that history plus the one operation that was in flight.
"""

from __future__ import annotations

import argparse
import io
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from oracle import OracleError, judge

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "cli.py"
ONE_MB = 1024 * 1024

MODES = (
    "default",
    "large-values",
    "large-batches",
    "compaction",
    "recovery-crash",
    "disk-full",
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="torture")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--mode", choices=MODES, default="default")
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        print("iterations must be at least 1", file=sys.stderr)
        return 2

    work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="kvdb-torture-"))
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "data.kv"
    oracle_path = work / "oracle.log"
    print(
        f"torture mode={args.mode} iterations={args.iterations} seed={args.seed} "
        f"kill=TerminateProcess work={work}",
        flush=True,
    )
    baseline: dict[str, str] = {}
    passed = 0
    try:
        for iteration in range(1, args.iterations + 1):
            rng = random.Random(_mix(args.seed, iteration, 0))
            if args.mode == "recovery-crash":
                baseline = _iteration_recovery(iteration, rng, db_path, oracle_path, baseline, args)
            else:
                baseline = _iteration_serve(iteration, rng, db_path, oracle_path, baseline, args)
            passed += 1
        print(f"PASS {passed} FAIL 0 seed={args.seed} mode={args.mode}", flush=True)
        return 0
    except (OracleError, RuntimeError) as exc:
        print(f"FAIL at iteration {passed + 1} seed={args.seed} mode={args.mode}", flush=True)
        print(exc, flush=True)
        print(f"pass={passed} fail=1", flush=True)
        print(f"oracle={oracle_path}", flush=True)
        return 1


def _mix(seed: int, iteration: int, writer: int) -> int:
    return (seed * 1_000_003 + iteration * 97 + writer * 17) & 0x7FFFFFFF


def _iteration_serve(iteration, rng, db_path, oracle_path, baseline, args):
    delay_ms = rng.randint(*_delay_range(args.mode))
    max_bytes = 4096 if args.mode == "disk-full" else None
    proc, stderr_lines = _spawn(["serve", str(db_path)], max_bytes)
    submitted = []
    results = {}
    ready = threading.Event()
    stop = threading.Event()
    send_lock = threading.Lock()
    state_lock = threading.Lock()

    def reader():
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith("READY"):
                ready.set()
            elif line.startswith("ACK "):
                op_id = line.split(" ", 2)[1]
                with state_lock:
                    results[op_id] = "ACK"
                _log_oracle(oracle_path, f"ACK {op_id}")
            elif line.startswith("ERR "):
                op_id = line.split(" ", 2)[1]
                with state_lock:
                    results[op_id] = "ERR"
                _log_oracle(oracle_path, f"ERR {op_id}")

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    threading.Thread(target=_drain_stderr, args=(proc, stderr_lines), daemon=True).start()
    if not ready.wait(60):
        proc.kill()
        raise RuntimeError("serve did not print READY within 60 seconds\n" + "".join(stderr_lines))

    writers = [
        threading.Thread(
            target=_writer,
            args=(wid, iteration, args, proc, submitted, send_lock, state_lock, stop),
        )
        for wid in (1, 2)
    ]
    for thread in writers:
        thread.start()
    time.sleep(delay_ms / 1000.0)
    if proc.poll() is None:
        proc.kill()
    stop.set()
    for thread in writers:
        thread.join(timeout=10)
    _finish_child(proc)
    reader_thread.join(timeout=5)
    verdict = _inspect(db_path, baseline, submitted, results, max_bytes)
    print(
        f"iter={iteration} delay_ms={delay_ms} submitted={len(submitted)} "
        f"acks={sum(1 for v in results.values() if v == 'ACK')} "
        f"match={verdict['match']} check=OK",
        flush=True,
    )
    return verdict["state"]


def _iteration_recovery(iteration, rng, db_path, oracle_path, baseline, args):
    """Write a little data cleanly, then kill the process while open() is running."""
    baseline = _clean_writes(iteration, rng, db_path, oracle_path, baseline)
    delay_ms = rng.randint(0, 30)
    # Several kills, including an immediate one. A tiny file can finish open()
    # before a long sleep, so the immediate kill is what lands inside startup.
    for wait_ms in (delay_ms, 0, 0, 0):
        proc, stderr_lines = _spawn(["open", str(db_path)], None)
        threading.Thread(target=_drain_stderr, args=(proc, stderr_lines), daemon=True).start()
        if wait_ms:
            time.sleep(wait_ms / 1000.0)
        if proc.poll() is None:
            proc.kill()
        _finish_child(proc)
    verdict = _inspect(db_path, baseline, [], {}, None)
    print(
        f"iter={iteration} recovery_kill_ms={delay_ms} match={verdict['match']} check=OK",
        flush=True,
    )
    return verdict["state"]


def _clean_writes(iteration, rng, db_path, oracle_path, baseline):
    proc, stderr_lines = _spawn(["serve", str(db_path)], None)
    submitted = []
    results = {}
    ready = threading.Event()
    state_lock = threading.Lock()

    def reader():
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith("READY"):
                ready.set()
            elif line.startswith("ACK "):
                op_id = line.split(" ", 2)[1]
                with state_lock:
                    results[op_id] = "ACK"
                _log_oracle(oracle_path, f"ACK {op_id}")

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    threading.Thread(target=_drain_stderr, args=(proc, stderr_lines), daemon=True).start()
    if not ready.wait(60):
        proc.kill()
        raise RuntimeError("serve did not print READY\n" + "".join(stderr_lines))
    assert proc.stdin is not None
    for n in range(4):
        op = {
            "id": f"{iteration}-c{n}",
            "kind": "put",
            "key": f"c{rng.randrange(8)}",
            "value": f"clean-{iteration}-{n}",
        }
        proc.stdin.write(f"put {op['id']} {op['key']} {op['value']}\n")
        proc.stdin.flush()
        submitted.append(op)
    deadline = time.time() + 20
    while time.time() < deadline and any(op["id"] not in results for op in submitted):
        time.sleep(0.01)
    proc.stdin.close()
    _finish_child(proc)
    reader_thread.join(timeout=5)
    if any(op["id"] not in results for op in submitted):
        raise RuntimeError("clean writes were not acknowledged before the recovery kill")
    verdict = judge(baseline, submitted, results, _dump_state(_run_dump(db_path)))
    return verdict["state"]


def _writer(wid, iteration, args, proc, submitted, send_lock, state_lock, stop):
    rng = random.Random(_mix(args.seed, iteration, wid))
    cap = _op_cap(args.mode)
    sent = 0
    while sent < cap and not stop.is_set():
        op = _make_op(rng, args.mode, f"{iteration}-{wid}-{sent}")
        try:
            text = _render(op)
            with send_lock:
                if stop.is_set() or proc.poll() is not None:
                    return
                assert proc.stdin is not None
                proc.stdin.write(text)
                proc.stdin.flush()
                with state_lock:
                    submitted.append(op)
        except (BrokenPipeError, OSError, ValueError):
            return
        sent += 1


def _make_op(rng: random.Random, mode: str, op_id: str) -> dict:
    if mode == "large-values":
        return {"id": op_id, "kind": "put", "key": f"big{rng.randrange(3)}", "value": "A" * ONE_MB}
    if mode == "large-batches":
        steps = []
        for i in range(10_000):
            steps.append({"kind": "put", "key": f"b{i % 200}", "value": f"{op_id}-{i}"})
        return {"id": op_id, "kind": "batch", "steps": steps}
    if mode == "compaction" and rng.randrange(5) == 0:
        return {"id": op_id, "kind": "compact"}
    roll = rng.randrange(10)
    if roll < 2:
        return {"id": op_id, "kind": "del", "key": f"k{rng.randrange(20)}"}
    if roll < 4:
        steps = []
        for _ in range(rng.randint(2, 6)):
            if rng.randrange(4) == 0:
                steps.append({"kind": "del", "key": f"k{rng.randrange(20)}"})
            else:
                steps.append(
                    {
                        "kind": "put",
                        "key": f"k{rng.randrange(20)}",
                        "value": f"v{rng.randrange(1000)}",
                    }
                )
        return {"id": op_id, "kind": "batch", "steps": steps}
    width = 180 if mode == "disk-full" else 12
    return {
        "id": op_id,
        "kind": "put",
        "key": f"k{rng.randrange(20)}",
        "value": "x" * width if mode == "disk-full" else f"v{rng.randrange(1000)}",
    }


def _render(op: dict) -> str:
    if op["kind"] == "put":
        return f"put {op['id']} {op['key']} {op['value']}\n"
    if op["kind"] == "del":
        return f"del {op['id']} {op['key']}\n"
    if op["kind"] == "compact":
        return f"compact {op['id']}\n"
    lines = [f"batch {op['id']} {len(op['steps'])}\n"]
    for step in op["steps"]:
        if step["kind"] == "put":
            lines.append(f"put {step['key']} {step['value']}\n")
        else:
            lines.append(f"del {step['key']}\n")
    return "".join(lines)


def _inspect(db_path, baseline, submitted, results, max_bytes):
    opened = _run_open(db_path, max_bytes)
    if opened.returncode != 0:
        raise RuntimeError(f"open failed\n{opened.stderr}\n{opened.stdout}")
    checked = _call_cli(["check", str(db_path)])
    if checked.returncode != 0 or checked.stdout.strip() != "OK":
        raise RuntimeError(f"check failed\n{checked.stdout}\n{checked.stderr}")
    dumped = _run_dump(db_path)
    try:
        return judge(baseline, submitted, results, _dump_state(dumped))
    except OracleError as exc:
        raise OracleError(
            f"{exc}\nopen: {opened.stdout.strip()}\nsubmitted ids: "
            + ", ".join(op["id"] for op in submitted)
        ) from exc


def _spawn(command, max_bytes):
    cmd = [sys.executable, "-u", str(CLI), *command]
    if max_bytes is not None:
        cmd.extend(["--max-bytes", str(max_bytes)])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=str(ROOT),
    )
    return proc, []


def _drain_stderr(proc, sink):
    if proc.stderr is None:
        return
    for line in proc.stderr:
        sink.append(line)


def _finish_child(proc):
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)
    if proc.stdin is not None and not proc.stdin.closed:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _run_open(db_path, max_bytes):
    args = ["open", str(db_path)]
    if max_bytes is not None:
        args.extend(["--max-bytes", str(max_bytes)])
    last = None
    for _ in range(20):
        last = _call_cli(args)
        if last.returncode == 0:
            return last
        if "locked" not in (last.stderr or ""):
            return last
        time.sleep(0.1)
    return last


def _run_dump(db_path):
    result = _call_cli(["dump", str(db_path)])
    if result.returncode != 0:
        raise RuntimeError(f"dump failed\n{result.stderr}")
    return result.stdout


class _CliResult:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _call_cli(args):
    """Run the same cli.main the judge runs, inside this process.

    open, check, and dump still happen, in that order, at the call sites.
    Skipping a new Python startup keeps 200 iterations inside the demo window.
    The killed database is still a separate process.
    """
    sys.path.insert(0, str(ROOT / "src"))
    import cli

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(args)
    return _CliResult(code, out.getvalue(), err.getvalue())


def _dump_state(text: str) -> dict:
    state = {}
    if text == "":
        return state
    for line in text.splitlines():
        key, value = line.split("\t", 1)
        state[_unescape(key)] = _unescape(value)
    return state


def _unescape(text: str) -> str:
    out = bytearray()
    i = 0
    while i < len(text):
        if text[i] != "\\":
            out.append(ord(text[i]))
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "\\":
            out.append(0x5C)
            i += 2
        elif nxt == "n":
            out.append(0x0A)
            i += 2
        elif nxt == "r":
            out.append(0x0D)
            i += 2
        elif nxt == "t":
            out.append(0x09)
            i += 2
        elif nxt == "x":
            out.append(int(text[i + 2 : i + 4], 16))
            i += 4
        else:
            raise RuntimeError(f"bad escape in dump: {text[i:i+4]!r}")
    return out.decode("utf-8")


def _log_oracle(path: Path, line: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _delay_range(mode: str) -> tuple:
    if mode == "large-values":
        return (30, 250)
    if mode == "large-batches":
        return (30, 600)
    if mode == "compaction":
        return (10, 400)
    if mode == "disk-full":
        return (5, 150)
    return (5, 80)


def _op_cap(mode: str) -> int:
    if mode == "large-values":
        return 20
    if mode == "large-batches":
        return 3
    if mode == "compaction":
        return 30
    return 40


if __name__ == "__main__":
    sys.exit(main())
