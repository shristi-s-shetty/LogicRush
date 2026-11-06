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
4. The project lives under OneDrive. Sync clients can lock the file or delay durability. Demo from a normal local folder.
5. `check` runs after `open`, and `open` already truncates a torn tail. So `check` on our CLI sees a repaired file. It does not detect bit rot in the middle of an old block. The problem does not require bit-rot repair.
6. The other torture modes (large values, large batches, disk full, recovery crash, compaction-during-serve) were measured on the previous format. The current format has the 500-kill default run and the replace-pause tests.

## 4. What we tried that failed

These runs passed. They are split by file format so an old number is not quoted as a result for the current code.

Version 2 (one `TXN1` block, one `os.fsync`), 6 Nov 2026:

- `python torture/torture.py --iterations 500 --seed 12345` -> `PASS 500 FAIL 0` in 221 seconds. Kill is `TerminateProcess`. Two writer threads. The oracle records a line only when it reads `ACK`. Most iterations were `with-inflight`. A shorter 200-iteration run on the same format also passed, in 69 seconds.
- `python torture/bench_recover.py --keys 1000000` -> `recovery_ms=1748.2` and, on a second run, `recovery_ms=1743.7`. File 16.0 MB. Sample keys matched.
- `tests/test_replace_kill.py` -> both kills passed: pause before `os.replace`, and pause after it.

Earlier format (a separate frame per operation, two fsyncs). These numbers do not apply to the current files:

- `--iterations 500 --seed 12345` -> `PASS 500 FAIL 0` in 269 seconds.
- `--iterations 4 --seed 999 --mode disk-full` -> pass. Later iterations had `acks=0` and the old keys stayed.
- `--iterations 4 --seed 7 --mode compaction` -> pass. That did not aim the kill at `os.replace`.
- `--iterations 3 --seed 42 --mode recovery-crash` -> pass after the mode was strengthened to four `open` kills per iteration.
- `--iterations 1 --seed 3 --mode large-values` -> 12 acknowledged 1 MB puts survived; the 13th was absent.
- `--iterations 1 --seed 11 --mode large-batches` -> two acknowledged batches of 10,000 puts survived; the third was absent.

A two-pass scan of the million-key file measured 2244.8 ms and was rejected because it misses the 2 second line. The current scanner records offsets, then updates the dictionary.

## 5. If we had 24 more hours

1. Repeat the other torture modes (large values, large batches, disk full, recovery crash, compaction) on this file format. The default 500-kill run and the replace-pause tests are the current evidence.
2. A kill inside the `os.replace` syscall, rather than on the pause before it and the pause after it.
3. Move the demo copy off OneDrive onto a normal local disk.
