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

`put`, `del`, and `batch` return only after `os.fsync` of the commit marker. A batch is visible only when that marker is valid. See `ARCHITECTURE.md` for the byte layout and the recovery steps.

## Limits

- Keys: 1 to 1,024 bytes.
- Values: 0 to 1 MB. An empty value is allowed.
- One batch: at most 10,000 operations.
- `--max-bytes N` refuses a write that would make the data file larger than N. The temporary compaction file is separate.

## What is not proven yet

Unit tests cover put/get/delete, batch order, a torn tail, a one-byte chop, disk-full refusal, two threads, and compaction shrink. They do **not** yet kill the process with `TerminateProcess` 500 times. Do not tell a judge this is crash-proven until `torture/` has been run. That tool is the next piece of work.

The project folder is under OneDrive. OneDrive can lock files and delay disk writes. For the demo, copy the repository to a normal local folder such as `C:\kvdemo` if kills start behaving strangely.
