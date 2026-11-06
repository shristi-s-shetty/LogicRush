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
6. There is no torture tool yet, so group F is not demonstrated.

## 4. What we tried that failed

Nothing has failed in the 20 unit tests run on 6 Nov 2026. That sentence is not a victory. Those tests chop a file by hand. They do not kill a running process. The empty space here is the missing torture tool, and it is the next thing we build.

## 5. If we had 24 more hours

1. A torture tool that kills with `TerminateProcess` at a random millisecond, with an oracle that records only real ACK lines, and seeds.
2. A timed open of 1,000,000 small keys, and a compaction before that open if the replay is slow.
3. A kill during `os.replace`, and a kill during `_recover`'s truncate, with the same seed printed on failure.
