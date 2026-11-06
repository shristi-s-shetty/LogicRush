# LogicRush KVDB

A key-value database in one file. It is written for the Kill It and Prove It hackathon.

A key is the label on a folder. A value is the paper inside. `put` stores the paper. `get` reads it. `del` throws the folder away. `batch` is a stack of changes that must all land together, or not land at all.

The file is the truth. The dictionary in memory is only a contents page. We rebuild that page from the file every time we open it.

## Build

No packages to install. You need Python 3.

```bat
python -m unittest discover -s tests -v
```

## Run

From the repository root:

```bat
python src\cli.py open  data.kv
python src\cli.py put   data.kv apple red
python src\cli.py get   data.kv apple
python src\cli.py del   data.kv apple
python src\cli.py batch data.kv ops.txt
python src\cli.py dump  data.kv
python src\cli.py check data.kv
python src\cli.py serve data.kv --max-bytes 50000000
```

`kvdb.bat` runs the same commands:

```bat
kvdb.bat get data.kv apple
```

`ops.txt` has one operation per line. The key is the first word. The value is the rest of the line, so a value may contain spaces.

```text
put apple red fruit
put empty
del apple
```

`get` prints the value, or `NOTFOUND`. `dump` prints one record per line, sorted by key: escaped key, a tab, escaped value. Newlines inside a value are shown as `\n`.

`open` prints `keys=<count> recovery_ms=<milliseconds>`.

`check` prints `OK` or a reason.

`serve` reads one operation per line and prints `ACK <id>` only after the commit marker has been fsynced:

```text
put 1 apple red
del 2 apple
batch 3 2
put a 1
del b
compact 4
```

The first line from serve is `READY <keys> <recovery_ms>`.

## What is durable

`put`, `del`, and `batch` return only after `os.fsync` of one `TXN1` block. That block holds every operation in the batch and one checksum of the whole body. Recovery keeps the batch only when the checksum matches. See `ARCHITECTURE.md` for the byte layout.

## Limits

- Keys: 1 to 1,024 bytes.
- Values: 0 to 1 MB. An empty value is allowed.
- One batch: at most 10,000 operations.
- `--max-bytes N` refuses a write that would make the data file larger than N. The temporary compaction file is separate.

## Torture tool

The referee is a separate program. It does not trust the database to grade itself. It writes an oracle log only when it reads `ACK` from the child process. The kill is `subprocess.Popen.kill()`, which on Windows is `TerminateProcess`.

```bat
python torture\torture.py --iterations 500 --seed 12345
python torture\torture.py --iterations 100 --seed 999 --mode large-values
python torture\torture.py --iterations 100 --seed 7 --mode compaction
python torture\torture.py --iterations 100 --seed 42 --mode recovery-crash
python torture\torture.py --iterations 3 --seed 42 --mode recovery-large --keys 250000
python torture\torture.py --iterations 100 --seed 42 --mode disk-full
python torture\torture.py --iterations 100 --seed 7 --mode large-batches
```

`--seed` is required. On this version-2 file, `--iterations 500 --seed 12345` finished `PASS 500 FAIL 0` in 221 seconds on 6 Nov 2026. Some iterations matched `with-inflight`: the block was already on disk before the tool read `ACK`. That is scenario A4, and it is legal. An earlier file format also passed 500 kills, in 269 seconds. Quote the 221 second run for this code.

The database file for a torture run is created under the system temp folder, not under OneDrive.

## What is not proven yet

Unit tests cover put/get/delete, batch order, a torn tail, a one-byte chop, disk-full refusal, two threads, and compaction shrink. The 500-kill run above is a process kill, not a power cut. Opening 1,000,000 keys measured 1748.2 ms and 1743.7 ms (`python torture/bench_recover.py --keys 1000000`, file 16.0 MB). On this same format, 6 Nov 2026: `disk-full` seed 999, 8 iterations, pass; `recovery-crash` seed 42, 6 iterations, pass on a tiny file; `recovery-large` seed 42, 250,000 keys, 3 iterations, pass, with 3 kills per iteration after the scan had started; `compaction` seed 7, 6 iterations, pass (this does not aim the kill at `os.replace`); `large-values` seed 3, 2 iterations, pass, and the one unacknowledged 1 MB put was absent; `large-batches` seed 11, 2 iterations, pass, and both in-flight batches of 10,000 were fully present. `tests/test_replace_kill.py` kills the process on the pause immediately before `os.replace` and on the pause immediately after it. The kill is not inside the kernel call itself.

The project folder is under OneDrive. OneDrive can lock files and delay disk writes. For the demo, copy the repository to a normal local folder such as `C:\kvdemo` if kills start behaving strangely.
