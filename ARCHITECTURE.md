# Architecture

This file describes the code in `src/kvdb.py` as it is today. A row that says "not verified" has not been killed with TerminateProcess.

## Diagram

See `diagrams/architecture.txt`.

```text
header 64 bytes
then frames, packed back to back
each frame: magic, length, kind, txn id, payload, crc32
a transaction is real only after a COMMIT frame with a matching crc
```

## File format

Integers are unsigned little-endian.

Header, 64 bytes:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | ASCII `KVDB` |
| 4 | 4 | version `1` |
| 8 | 4 | flags `0` |
| 12 | 4 | header length `64` |
| 16 | 8 | generation, starts at 1, increases on compaction |
| 24 | 8 | max_bytes, `0` means no cap |
| 32 | 8 | committed-offset hint, not trusted over a scan |
| 40 | 20 | zeros |
| 60 | 4 | CRC32 of the first 60 bytes |

Frame:

| Size | Field |
| --- | --- |
| 4 | ASCII `FRM1` |
| 4 | frame length, including magic and the CRC |
| 2 | kind |
| 2 | flags, always 0 |
| 8 | transaction id |
| 4 | payload length |
| payload | kind-specific |
| 4 | CRC32 of every byte before the CRC |

Kinds: `1` PUT, `2` DEL, `3` COMMIT, `4` SNAPSHOT_START, `5` SNAPSHOT_END, `6` META (a new max_bytes).

PUT payload: key length, value length, key bytes, value bytes.
DEL payload: key length, key bytes.
COMMIT payload: operation count. It must equal the number of frames in that transaction.
SNAPSHOT_START payload: live key count.
SNAPSHOT_END payload: live key count and new generation.
META payload: the new max_bytes as an 8-byte integer.

A frame longer than about 1 MB plus a key is rejected. A short tail, a bad magic, or a bad CRC stops the scan. Those bytes are never returned as a value.

## Write sequence

`Database._write_transaction`:

1. Take the lock (the caller already holds it).
2. Build every data frame in memory. They share one transaction id.
3. Remember the file size. That is the checkpoint.
4. If `max_bytes` is set and the frames plus the commit would pass it, raise `DiskFullError` and write nothing.
5. Append the data frames.
6. `flush`, then `os.fsync`.
7. Append the COMMIT frame.
8. `flush`, then `os.fsync`.
9. Update the memory dictionary.
10. Return.

If step 5 or 7 raises, we truncate back to the checkpoint and fsync that truncation.

`serve` prints `ACK` only after `put`, `delete`, `batch`, or `compact` returns. Those methods return only after step 8.

## Recovery sequence

`Database._recover` on every `open`:

1. Read the whole file.
2. Check the header magic, version, and CRC. A bad header raises. We do not guess.
3. Delete `path.compacting` if it exists. The real file is already valid, so the temp is leftover.
4. Walk frames from byte 64.
5. Hold PUT, DEL, and META frames in a pending list. Do not touch the dictionary yet.
6. On a valid COMMIT whose count matches the pending list, apply the list and remember this end offset.
7. A SNAPSHOT_END whose count matches replaces the dictionary with that snapshot.
8. On the first frame we cannot parse, stop.
9. If the file is longer than the last good offset, truncate to that offset and fsync.
10. The dictionary now holds only committed data.

Opening again is the same result, because the torn tail is already gone. That is what `test_torn_tail_is_ignored_and_second_open_is_stable` checks.

## Locking

One `threading.RLock` covers get, put, delete, batch, check, recovery, and compaction. A get cannot run in the middle of a batch, so it sees the dictionary from before the batch or after it.

A sibling `path.lock` file is locked with `msvcrt.locking` so a second process fails instead of writing the same file. The torture tool will still use one serve process and two writer threads.

## Compaction

`Database.compact` writes a new file `path.compacting`: header, SNAPSHOT_START, one PUT per live key, SNAPSHOT_END, then fsync. It closes the live handle and `os.replace`s the temp onto the real path. Then it recovers the new file.

A crash before `os.replace` leaves the old file. The next open deletes the temp. This is tested by planting a junk temp file, not yet by killing the process during `os.replace`.

## The three guarantees, as far as the code goes

Durability: `ACK` and a normal return happen after the second `os.fsync` in `_write_transaction`. A commit marker that is fully on disk is applied by `_recover` even if the caller never saw the return. `test_complete_commit_marker_is_visible_even_without_a_return` writes that marker and reopens. This is not a process kill.

Atomicity: `_recover` applies a transaction only when the COMMIT count matches. Three data frames and no COMMIT are dropped. `test_frames_without_commit_marker_stay_invisible` and `test_batch_that_does_not_fit_is_fully_absent` cover that. A kill in the middle of a live `batch()` call is not verified yet.

Recoverability: a torn tail truncates to the previous commit, and a second open sees a clean file. A bad header still raises. We have not killed `open` itself.

## Requirement-to-proof table

| Scenario | Disk state if we die there | What handles it | Code | Verified by |
| --- | --- | --- | --- | --- |
| A1 | No new bytes | Recovery keeps the old dictionary | `_recover` | not a process kill; a put that raises before write leaves the old key, see disk-full tests |
| A2 | A partial frame, bad length or CRC | `try_parse_frame` returns None, tail truncated | `try_parse_frame`, `_recover` | `test_torn_tail_is_ignored_and_second_open_is_stable` |
| A3 | Data frames fsynced, commit missing or not fsynced | No COMMIT means not applied | `_write_transaction`, `_recover` | `test_frames_without_commit_marker_stay_invisible`. Either outcome is allowed if the commit later landed |
| A4 | COMMIT fsynced, function has not returned | Recovery applies the commit | `_recover` | `test_complete_commit_marker_is_visible_even_without_a_return` (file written, then open; not a kill) |
| A5 | COMMIT fsynced and put returned | Same as A4 | `_write_transaction` | `test_put_get_delete_and_reopen` |
| A6 | A DEL frame with or without COMMIT | Old value, or key absent. CRC blocks a torn value | `_recover` | partial: delete-and-reopen test. Kill during delete not run |
| A7 | Same as A1-A5 with a 1 MB payload | Same parser | `pack_frame` | not verified at 1 MB yet |
| B1 | 3 of 10 data frames, no COMMIT | Pending frames discarded | `_recover` | same mechanism as the no-commit test, not a 10-op kill |
| B2 | 10 data frames, no COMMIT | Nothing visible | `_recover` | `test_frames_without_commit_marker_stay_invisible` |
| B3 | COMMIT bytes started, fsync not done | All if the frame parses, else none | `try_parse_frame` | not verified with a kill |
| B4 | Batch returned | COMMIT is on disk | `_write_transaction` | `test_batch_applies_in_order_and_survives_reopen` |
| B5 | put X and del X in one batch | Applied in list order on commit | `_apply` | `test_put_then_delete_same_key_in_one_batch` |
| B6 | 10,000 ops, killed halfway | No commit, so none visible | `_recover` | not verified |
| C1 | Kill during replay | Replay only reads, then truncates a tail | `_recover` | second open is stable in the torn-tail test. Kill during open not run |
| C2 | Kill during truncate | Next open truncates again to the same commit | `_recover` | not a kill during truncate |
| C3 | Temp file partial, old file untouched | Open deletes temp | `_discard_leftover_temp` | `test_leftover_compaction_file_is_removed_and_old_data_remains` |
| C4 | Kill during `os.replace` | One of the two complete files remains | `compact` | not verified |
| C5 | Kill after replace, before temp cleanup | `os.replace` removes the temp name | `compact` | not a kill; leftover temp before replace is tested |
| C6 | Kill during close | close fsyncs first; a kill is the same rule as any other | `close` | not verified |
| D1 | Two writers, different keys | Lock plus commit per put | `batch`, lock | `test_two_writers_do_not_mix_values` (no kill) |
| D2 | Two writers, same key | Lock; value is one full value | lock, `_apply` | same test, `shared` is `A` or `B` |
| D3 | Batch plus single puts | Lock; batch commits as one | `batch` | not a kill |
| D4 | Reader during batch | Lock; reader sees before or after | `get`, `batch` | `test_reader_during_batch_sees_before_or_after` |
| E1 | Cap hit on one put | Error before write | `_write_transaction` | `test_put_that_does_not_fit_leaves_old_data` |
| E2 | Cap hit on a batch | Nothing from that batch written | `_write_transaction` | `test_batch_that_does_not_fit_is_fully_absent` |
| E3 | Cap hit, then killed | No partial commit was written | same | not a kill |
| E4 | Cap hit during compaction | Compaction raises, old file stays if replace did not happen | `compact` | not verified under a kill |
| E5 | Deletes, compact, write again | Snapshot drops dead keys | `compact` | `test_deletes_and_compaction_allow_writes_again` |
| F1 | 500 kills | Would use the torture tool | not built | not verified |
| F2 | Kill during recovery twice | Recovery truncates then only reads | `_recover` | second open after a torn tail, not a kill |
| F3 | 1,000,000 keys under 2 seconds | Not measured | `_recover` | not verified |
| F4 | File shortened by 1 byte | Last commit fails CRC, previous commit kept | `_recover` | `test_one_byte_chop_loses_only_the_unfinished_tail` |
