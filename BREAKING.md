# How this database can still break

## 1. Crash points

| Where | If we die there, what is on disk | Next open |
| --- | --- | --- |
| `_write_transaction`, before `write(body)` | Old commits only | Old keys |
| After `write(body)`, before the first fsync returns | Data frames may be partial | Torn frame ignored, truncated |
| After the first fsync, before `write(commit)` | Full data frames, no COMMIT | Transaction invisible |
| After `write(commit)`, before the second fsync returns | COMMIT may be partial | All of the transaction if the CRC matches, else none |
| After the second fsync, before `return` | Full COMMIT | Transaction visible. This is scenario A4 |
| `serve`, after return, before `print ACK` | Same as a successful return | Visible, even if the tool missed the ACK. The tool must only record an ACK it actually read |
| `_recover`, during `truncate` | Either the long file or the already short file | Both share the same committed prefix |
| `compact`, before `os.replace` | Old file complete, temp maybe partial | Old file. Temp deleted |
| `compact`, inside `os.replace` | One complete file, old or new | Whichever name won |
| `close`, during its fsync | Last commit already fsynced by the write path | Same as any other kill |

## 2. Ordering argument

The data frames are fsynced before the COMMIT frame is written. A COMMIT whose CRC matches therefore sits after a complete payload. Recovery applies a transaction only when that COMMIT's operation count matches the pending frames. `os.fsync` is called on the data file, not on the directory. For a process kill after fsync, the problem says the bytes are on disk. We do not fsync the directory. We have not shown that a power cut would keep the directory entry. `os.replace` is what swaps the compaction file. We are relying on NTFS replace of two files on the same volume being one name change. That is not yet killed in a test.

## 3. Known weaknesses

1. Recovery reads the whole file and rebuilds a Python dictionary. A long log of 1,000,000 keys has not been timed. It may miss the 2 second line until we compact first and measure.
2. One global lock means a second writer waits for fsync. That is allowed, and it makes large batches slow.
3. `os.replace` during compaction has not been killed. A failed replace on OneDrive could be different from a local NTFS disk.
4. The project lives under OneDrive. Sync clients can lock the file or delay durability. Demo from a normal local folder.
5. `check` runs after `open`, and `open` already truncates a torn tail. So `check` on our CLI sees a repaired file. It does not detect bit rot in the middle of an old commit. The problem does not require bit-rot repair.
6. One million keys have not been timed. Group F1 has now been run; F3 has not.

## 4. What we tried that failed

On 6 Nov 2026 the torture tool passed these runs. Nothing failed, so this section records what the runs actually were, not a fake bug.

- `python torture/torture.py --iterations 500 --seed 12345` -> `PASS 500 FAIL 0` in 269 seconds. Kill is `TerminateProcess`. Two writer threads. Oracle records only lines that start with `ACK`. Several iterations were `with-inflight`: the killed operation had reached disk before we saw `ACK`.
- `--iterations 4 --seed 999 --mode disk-full` -> pass. After the file hit the cap, later iterations had `acks=0` and the old keys were still intact.
- `--iterations 4 --seed 7 --mode compaction` -> pass, including kills while compact commands were in the stream. This does not prove the kill landed inside `os.replace`.
- `--iterations 4 --seed 42 --mode recovery-crash` -> pass. After that, the mode was changed to kill `open` four times per iteration, including an immediate kill. A fresh `--iterations 3 --seed 42 --mode recovery-crash` also passed.
- `--iterations 1 --seed 3 --mode large-values` -> 12 acknowledged 1 MB puts survived; the 13th was in flight and absent (`without-inflight`).
- `--iterations 1 --seed 11 --mode large-batches` -> two acknowledged batches of 10,000 puts survived; the third batch was in flight and absent.

The hand-chop unit tests still exist. They are a different test from process kill.

## 5. If we had 24 more hours

1. Time `open` on 1,000,000 small keys, and compact first if the replay is slower than 2 seconds.
2. A kill that is aimed at the `os.replace` line, not just at a serve process that sometimes happens to be compacting.
3. Move the demo copy off OneDrive onto a normal local disk.
