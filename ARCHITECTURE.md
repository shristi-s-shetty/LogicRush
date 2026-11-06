# Architecture

This file describes the code in `src/kvdb.py` as it is today. A row that says "not verified" has not been killed with TerminateProcess.

## Diagram

See `diagrams/architecture.txt`.

```text
header 64 bytes
then blocks, packed back to back
a TXN1 block is one batch: every operation, then one CRC of that body
the block is real only when the CRC matches and the body is all there
```

## File format

Integers are unsigned little-endian. Version is 2.

Header, 64 bytes:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | ASCII `KVDB` |
| 4 | 4 | version `2` |
| 8 | 4 | flags `0` |
| 12 | 4 | header length `64` |
| 16 | 8 | generation, starts at 1, increases on compaction |
| 24 | 8 | max_bytes, `0` means no cap |
| 32 | 8 | committed-offset hint, not trusted over a scan |
| 40 | 20 | zeros |
| 60 | 4 | CRC32 of the first 60 bytes |

A normal write is one `TXN1` block, 24-byte head plus body:

| Size | Field |
| --- | --- |
| 4 | ASCII `TXN1` |
| 8 | transaction id |
| 4 | operation count |
| 4 | body length |
| 4 | CRC32 of the body only |
| body | the operations |

Each operation inside the body:

| Kind byte | What follows |
| --- | --- |
| `1` put | key length u16, value length u32, key bytes, value bytes |
| `2` delete | key length u16, key bytes |
| `6` new cap | the new max_bytes as u64 |

A compaction writes one `SNP1` block instead, 36-byte head plus body:

| Size | Field |
| --- | --- |
| 4 | ASCII `SNP1` |
| 8 | transaction id |
| 8 | new generation |
| 8 | live key count |
| 4 | body length |
| 4 | CRC32 of the body |
| body | one put record per live key, same layout as kind `1` |

The checksum is the seal. If the kill cuts the block, the length is short or the CRC does not match, and recovery ignores that whole block. A matching CRC means every operation in the block is present, so a batch cannot be half-applied.

## Write sequence

`Database._write_transaction`:

1. Take the lock (the caller already holds it).
2. Build one `TXN1` block in memory. The CRC covers the whole body.
3. Remember the file size. That is the checkpoint.
4. If `max_bytes` is set and the block would pass it, raise `DiskFullError` and write nothing.
5. Append the block.
6. `flush`, then `os.fsync`.
7. Update the memory dictionary.
8. Return.

If step 5 raises, we truncate back to the checkpoint and fsync that truncation.

`serve` prints `ACK` only after `put`, `delete`, `batch`, or `compact` returns. Those methods return only after step 6.

## Recovery sequence

`Database._recover` on every `open`:

1. Read the whole file.
2. Check the header magic, version, and CRC. A bad header raises. We do not guess.
3. Delete `path.compacting` if it exists. The real file is already valid, so the temp is leftover.
4. Walk blocks from byte 64.
5. A `TXN1` block is applied only when the body is fully present and the CRC matches.
6. A `SNP1` block replaces the dictionary with that snapshot, on the same rule.
7. On the first block we cannot validate, stop.
8. If the file is longer than the last good offset, truncate to that offset and fsync.
9. The dictionary now holds only sealed blocks.

Opening again is the same result, because the torn tail is already gone. That is what `test_torn_tail_is_ignored_and_second_open_is_stable` checks.

## Locking

One `threading.RLock` covers get, put, delete, batch, check, recovery, and compaction. A get cannot run in the middle of a batch, so it sees the dictionary from before the batch or after it.

A sibling `path.lock` file is locked with `msvcrt.locking` so a second process fails instead of writing the same file. The torture tool will still use one serve process and two writer threads.

## Compaction

`Database.compact` writes a new file `path.compacting`: a header and one `SNP1` block of the live keys, then fsync. It closes the live handle and `os.replace`s the temp onto the real path. Then it recovers the new file.

`os.replace` is one operating-system call, so a process kill is either before it or after it. `tests/test_replace_kill.py` starts `serve`, waits until the process prints `CRASH_AT before-replace` or `CRASH_AT after-replace`, and then calls `TerminateProcess`. Before the call, the old file is still authoritative. After the call, the new snapshot is. Both opens returned the key that had been acknowledged. The pause exists only when `KVDB_CRASH_AT` is set. Normal runs do not sleep there.

## The three guarantees, as far as the code goes

Durability: `ACK` and a normal return happen after `os.fsync` of the `TXN1` block in `_write_transaction`. A block whose CRC matches is applied by `_recover` even if the caller never saw the return. `test_complete_commit_marker_is_visible_even_without_a_return` writes that block and reopens. This is not a process kill.

Atomicity: `_recover` applies a block only when the CRC of the whole body matches. A short or torn block is dropped, so a batch cannot appear as a few of its keys. `test_frames_without_commit_marker_stay_invisible` and `test_batch_that_does_not_fit_is_fully_absent` cover that. The 500-kill run below also saw in-flight batches.

Recoverability: a torn tail truncates to the previous sealed block, and a second open sees a clean file. A bad header still raises. `recovery-crash` mode aims `TerminateProcess` at `open`. On a tiny file that open can finish before the kill, so the run shows the data is still intact afterwards. It does not prove the kill landed inside the replay.

## Requirement-to-proof table

| Scenario | Disk state if we die there | What handles it | Code | Verified by |
| --- | --- | --- | --- | --- |
| A1 | No new bytes | Recovery keeps the old dictionary | `_recover` | not a process kill; a put that raises before write leaves the old key, see disk-full tests |
| A2 | A partial block, short length or bad CRC | Scan stops, tail truncated | `_scan_records`, `_recover` | `test_torn_tail_is_ignored_and_second_open_is_stable` |
| A3 | Block bytes written, `os.fsync` has not returned | The block is visible only if its CRC matches; a torn block is absent | `_write_transaction`, `_recover` | `test_frames_without_commit_marker_stay_invisible`. Either outcome is allowed |
| A4 | Block fsynced, function has not returned | Recovery applies the block | `_recover` | `test_complete_commit_marker_is_visible_even_without_a_return` (file written, then open; not a kill) |
| A5 | Block fsynced and put returned | Same as A4 | `_write_transaction` | `test_put_get_delete_and_reopen` |
| A6 | A delete inside a sealed block, or a torn block | Old value, or key absent | `_recover` | delete-and-reopen test. A kill during delete is the same rule as A2–A5 |
| A7 | Same as A1–A5 with a 1 MB payload | Same parser | `pack_transaction` | `large-values` seed 3, 2 iterations, `PASS 2 FAIL 0`. First iteration: 12 submitted, 11 acknowledged, the unacknowledged 1 MB put was absent |
| B1 | Part of a batch block, CRC fails | Whole batch absent | `_recover` | same mechanism as the torn-block test |
| B2 | Batch body incomplete | Nothing from that batch visible | `_recover` | `test_frames_without_commit_marker_stay_invisible` |
| B3 | Block bytes started, fsync not done | All if the CRC matches, else none | `_scan_records` | covered by the torn-tail test, not by a live kill of this exact line |
| B4 | Batch returned | The `TXN1` block is on disk | `_write_transaction` | `test_batch_applies_in_order_and_survives_reopen` |
| B5 | put X and del X in one batch | Applied in list order after the block parses | `_apply_ops` | `test_put_then_delete_same_key_in_one_batch` |
| B6 | 10,000 ops, killed while the batch is in flight | The batch is all present or all absent | `_recover` | `large-batches` seed 11, `PASS 2 FAIL 0`. Both in-flight batches were fully present. No torn batch. The absent side was not observed in this run |
| C1 | Kill during replay | Replay only reads, then truncates a tail | `_recover` | `recovery-crash` seed 42, 6 iterations, `PASS 6 FAIL 0`. Four `open` kills per iteration. The file is small, so this shows the acknowledged keys survived. It does not prove the kill hit during the scan |
| C2 | Kill during truncate | Next open truncates again to the same commit | `_recover` | not a kill during truncate |
| C3 | Temp file partial, old file untouched | Open deletes temp | `_discard_leftover_temp` | `test_leftover_compaction_file_is_removed_and_old_data_remains` |
| C4 | Kill on either side of `os.replace` | The old file, or the new snapshot. The call itself is one syscall | `compact`, `_pause_for_crash` | `test_kill_before_replace_keeps_the_old_file` and `test_kill_after_replace_keeps_the_new_file` |
| C5 | Kill after replace, before temp cleanup | `os.replace` removes the temp name | `compact` | not a kill; leftover temp before replace is tested |
| C6 | Kill during close | close fsyncs first; a kill is the same rule as any other | `close` | not verified |
| D1 | Two writers, different keys | Lock plus commit per put | `batch`, lock | `test_two_writers_do_not_mix_values` (no kill) |
| D2 | Two writers, same key | Lock; value is one full value | lock, `_apply` | same test, `shared` is `A` or `B` |
| D3 | Batch plus single puts | Lock; batch commits as one | `batch` | not a kill |
| D4 | Reader during batch | Lock; reader sees before or after | `get`, `batch` | `test_reader_during_batch_sees_before_or_after` |
| E1 | Cap hit on one put | Error before write | `_write_transaction` | `test_put_that_does_not_fit_leaves_old_data` |
| E2 | Cap hit on a batch | Nothing from that batch written | `_write_transaction` | `test_batch_that_does_not_fit_is_fully_absent` |
| E3 | Cap hit, then killed | No partial commit was written | same | `disk-full` seed 999, 8 iterations, `PASS 8 FAIL 0`. After the cap, later iterations had `acks=0` and `match=exact` |
| E4 | Cap hit during compaction | Compaction raises, old file stays if replace did not happen | `compact` | not verified under a kill |
| E5 | Deletes, compact, write again | Snapshot drops dead keys | `compact` | `test_deletes_and_compaction_allow_writes_again` |
| F1 | 500 kills on this format | Torture tool, TerminateProcess, oracle of ACK lines | `torture/torture.py` | `PASS 500 FAIL 0` seed 12345 on 6 Nov 2026, 221 seconds |
| F2 | Kill during recovery more than once | Recovery truncates then only reads | `_recover` | same `recovery-crash` run as C1: four kills of `open` per iteration, then a clean open. Tiny file |
| F3 | 1,000,000 keys under 2 seconds | One scan of `TXN1` blocks into a dictionary | `_recover` | `torture/bench_recover.py --keys 1000000`: 1748.2 ms and 1743.7 ms, file 16.0 MB, on 6 Nov 2026. Both under 2000. Margin is a few hundred milliseconds, not a wide gap |
| F4 | File shortened by 1 byte | Last commit fails CRC, previous commit kept | `_recover` | `test_one_byte_chop_loses_only_the_unfinished_tail` |
