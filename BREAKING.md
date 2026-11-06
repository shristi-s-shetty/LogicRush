# How this database can still break

## 1. Crash points

| Where | If we die there, what is on disk | Next open |
| --- | --- | --- |
| `_write_transaction`, before `write` of the block | Old sealed blocks only | Old keys |
| After `write` of the `TXN1` block, before `os.fsync` returns | The block may be partial | Torn block ignored, tail truncated |
| After `os.fsync` returns, before `return` | Full block, CRC matches | Block visible. This is scenario A4 |
| `serve`, after return, before `print ACK` | Same as a successful return | Visible, even if the tool missed the ACK. The tool records an ACK only when it reads one |
| `_recover`, during `truncate` | Either the long file or the already short file | Both share the same sealed prefix |
| `compact`, before `os.replace` | Old file complete, temp maybe partial | Old file. Temp deleted. `test_kill_before_replace_keeps_the_old_file` kills here |
| `compact`, inside `os.replace` | One complete file, old or new | Whichever name won. We pause on the line before and the line after. We do not claim a kill in the middle of the syscall |
| `compact`, after `os.replace` returns | New snapshot | New file. `test_kill_after_replace_keeps_the_new_file` kills here |
| `close`, during its fsync | Last block already fsynced by the write path | Same as any other kill |

## 2. Ordering argument

One `TXN1` block holds every operation and one CRC of that body. `os.fsync` is called once on that block, on the data file, not on the directory. Recovery applies the block only when the CRC matches and the body parses completely. For a process kill after fsync, the problem says the bytes are on disk. We do not fsync the directory. We have not shown that a power cut would keep the directory entry. `os.replace` swaps the compaction file. We kill on the pause before that call and on the pause after it. We have not killed the thread inside the kernel during the call.

## 3. Known weaknesses

1. Opening 1,000,000 keys took 1748.2 ms and 1743.7 ms on 6 Nov 2026 (`python torture/bench_recover.py --keys 1000000`, file 16.0 MB). Both are under 2000 ms. The gap is a few hundred milliseconds. A slower laptop can miss it.
2. One global lock means a second writer waits for fsync. That is allowed, and it makes large batches slow.
3. The replace tests kill a 30 second pause that exists only when `KVDB_CRASH_AT` is set. They do not kill the `os.replace` syscall itself. A failed replace on OneDrive could be different from a local NTFS disk.
4. The project lives under OneDrive. Sync clients can lock the file or delay durability. Demo from `C:\kvdemo`.
5. `check` runs after `open`, and `open` already truncates a torn tail. So `check` on our CLI sees a repaired file. It does not detect bit rot in the middle of an old block. The problem does not require bit-rot repair.
6. `recovery-crash` uses a tiny file, so `open` can finish before `TerminateProcess`. `recovery-large` is the run that kills after the scan has started. `compaction` mode kills a serve process that sometimes compacts. It does not aim at `os.replace`. The large-batch run saw in-flight batches fully present, not absent.

## 4. What we tried that failed

These runs passed. They are split by file format so an old number is not quoted as a result for the current code.

Version 2 (one `TXN1` block, one `os.fsync`), 6 Nov 2026:

- `python torture/torture.py --iterations 500 --seed 12345` -> `PASS 500 FAIL 0` in 221 seconds. Kill is `TerminateProcess`. Two writer threads. The oracle records a line only when it reads `ACK`. Most iterations were `with-inflight`. A shorter 200-iteration run on the same format also passed, in 69 seconds.
- `python torture/bench_recover.py --keys 1000000` -> `recovery_ms=1748.2` and, on a second run, `recovery_ms=1743.7`. File 16.0 MB. Sample keys matched.
- `tests/test_replace_kill.py` -> both kills passed: pause before `os.replace`, and pause after it.
- `--iterations 8 --seed 999 --mode disk-full` -> `PASS 8 FAIL 0` in 16 seconds. Iteration 1 acknowledged 29 puts. Iterations 2 through 8 acknowledged 0 and still matched exactly, so the old keys stayed after the cap.
- `--iterations 6 --seed 42 --mode recovery-crash` -> `PASS 6 FAIL 0` in 14 seconds. Four kills of `open` per iteration on a tiny file. Every match was `exact`. That file can finish opening before the kill.
- `--iterations 3 --seed 42 --mode recovery-large --keys 250000` -> `PASS 3 FAIL 0` in 21 seconds, including the write. File 4.0 MB. Each iteration killed `open` 3 times after stderr printed `RECOVER_START`, and each time the process was still alive (`killed_during_scan=3`). `KVDB_MARK_RECOVER` only prints that line. It does not sleep. The following open reported `keys=250000`, `check` was OK, and the first and last keys were `v`. Those clean opens took 1033 ms, 961 ms, and 574 ms.
- A copy at `C:\kvdemo` (not under OneDrive) ran the 29 unit tests OK in 1.8 seconds, `get` returned `red` after `put apple red`, `check` printed `OK`, and `--iterations 2 --seed 12345` finished `PASS 2 FAIL 0`.
- `--iterations 6 --seed 7 --mode compaction` -> `PASS 6 FAIL 0` in 13 seconds. Kills during serve, not aimed at `os.replace`.
- `--iterations 2 --seed 3 --mode large-values` -> `PASS 2 FAIL 0` in 17 seconds. Iteration 1: 12 submitted, 11 acknowledged, the unacknowledged 1 MB put was absent (`without-inflight`). Iteration 2 finished both puts before the kill (`exact`).
- `--iterations 2 --seed 11 --mode large-batches` -> `PASS 2 FAIL 0` in 11 seconds. Iteration 1 acknowledged one batch of 10,000 and the next in-flight batch was fully present. Iteration 2 had no ACK and that one in-flight batch was fully present. No batch was torn.

Earlier format (a separate frame per operation, two fsyncs). These numbers do not apply to the current files:

- `--iterations 500 --seed 12345` -> `PASS 500 FAIL 0` in 269 seconds.
- `--iterations 4 --seed 999 --mode disk-full` -> pass. Later iterations had `acks=0` and the old keys stayed.
- `--iterations 4 --seed 7 --mode compaction` -> pass. That did not aim the kill at `os.replace`.
- `--iterations 3 --seed 42 --mode recovery-crash` -> pass after the mode was strengthened to four `open` kills per iteration.
- `--iterations 1 --seed 3 --mode large-values` -> 12 acknowledged 1 MB puts survived; the 13th was absent.
- `--iterations 1 --seed 11 --mode large-batches` -> two acknowledged batches of 10,000 puts survived; the third was absent.

A two-pass scan of the million-key file measured 2244.8 ms and was rejected because it misses the 2 second line. The current scanner records offsets, then updates the dictionary.

## 5. If we had 24 more hours

1. A kill inside the `os.replace` syscall, rather than on the pause before it and the pause after it. A process kill is on one side or the other of a single call, so this may stay unproven.
