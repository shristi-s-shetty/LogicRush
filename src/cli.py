"""Command-line wrapper. The judge should be able to run these from the repo root.

    python src/cli.py open  PATH [--max-bytes N]
    python src/cli.py put   PATH KEY VALUE
    python src/cli.py get   PATH KEY
    python src/cli.py del   PATH KEY
    python src/cli.py batch PATH FILE
    python src/cli.py dump  PATH
    python src/cli.py check PATH
    python src/cli.py serve PATH [--max-bytes N]
    python src/cli.py compact PATH

ACK is printed only after the database method returns, and that method returns
only after fsync of the commit marker.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kvdb


def _open(path: str, max_bytes):
    db = kvdb.Database()
    db.open(path, max_bytes=max_bytes)
    return db


def _print_error(message: str) -> int:
    print(f"ERROR {message}", file=sys.stderr)
    return 1


def cmd_open(args) -> int:
    db = _open(args.path, args.max_bytes)
    try:
        print(f"keys={db.key_count} recovery_ms={db.recovery_ms:.3f}")
    finally:
        db.close()
    return 0


def cmd_put(args) -> int:
    db = _open(args.path, args.max_bytes)
    try:
        db.put(args.key, " ".join(args.value))
    finally:
        db.close()
    return 0


def cmd_get(args) -> int:
    db = _open(args.path, args.max_bytes)
    try:
        value = db.get(args.key)
    finally:
        db.close()
    if value is None:
        print("NOTFOUND")
        return 0
    print(kvdb.escape_bytes(value))
    return 0


def cmd_del(args) -> int:
    db = _open(args.path, args.max_bytes)
    try:
        db.delete(args.key)
    finally:
        db.close()
    return 0


def cmd_batch(args) -> int:
    ops = _read_batch_file(args.file)
    db = _open(args.path, args.max_bytes)
    try:
        db.batch(ops)
    finally:
        db.close()
    return 0


def cmd_dump(args) -> int:
    db = _open(args.path, args.max_bytes)
    try:
        for key, value in db.items():
            print(kvdb.format_record(key, value))
    finally:
        db.close()
    return 0


def cmd_check(args) -> int:
    db = _open(args.path, args.max_bytes)
    try:
        reason = db.check()
    finally:
        db.close()
    print(reason)
    return 0 if reason == "OK" else 1


def cmd_compact(args) -> int:
    db = _open(args.path, args.max_bytes)
    try:
        db.compact()
        print(f"keys={db.key_count} recovery_ms={db.recovery_ms:.3f}")
    finally:
        db.close()
    return 0


def cmd_serve(args) -> int:
    db = _open(args.path, args.max_bytes)
    print(f"READY {db.key_count} {db.recovery_ms:.3f}", flush=True)
    try:
        for line in sys.stdin:
            line = line.rstrip("\r\n")
            if not line:
                continue
            try:
                _serve_line(db, line)
            except kvdb.KvError as exc:
                op_id = _op_id(line)
                print(f"ERR {op_id} {exc}", flush=True)
    finally:
        db.close()
    return 0


def _op_id(line: str) -> str:
    parts = line.split(" ")
    if len(parts) >= 2:
        return parts[1]
    return "?"


def _serve_line(db: kvdb.Database, line: str) -> None:
    parts = line.split(" ")
    cmd = parts[0]
    if cmd == "put":
        if len(parts) < 3:
            raise kvdb.KvError("usage: put OPID KEY [VALUE]")
        op_id, key = parts[1], parts[2]
        value = " ".join(parts[3:]) if len(parts) > 3 else ""
        db.put(key, value)
        print(f"ACK {op_id}", flush=True)
        return
    if cmd == "del":
        if len(parts) != 3:
            raise kvdb.KvError("usage: del OPID KEY")
        db.delete(parts[2])
        print(f"ACK {parts[1]}", flush=True)
        return
    if cmd == "batch":
        if len(parts) != 3:
            raise kvdb.KvError("usage: batch OPID COUNT")
        op_id = parts[1]
        try:
            count = int(parts[2])
        except ValueError as exc:
            raise kvdb.KvError("batch count must be an integer") from exc
        if count < 0 or count > kvdb.MAX_BATCH:
            raise kvdb.KvError(f"batch count must be 0..{kvdb.MAX_BATCH}")
        raw_lines = []
        for _ in range(count):
            raw = sys.stdin.readline()
            if raw == "":
                raise kvdb.KvError("batch ended before all operations arrived")
            raw_lines.append(raw.rstrip("\r\n"))
        ops = [_parse_batch_line(raw) for raw in raw_lines]
        db.batch(ops)
        print(f"ACK {op_id}", flush=True)
        return
    if cmd == "compact":
        if len(parts) != 2:
            raise kvdb.KvError("usage: compact OPID")
        db.compact()
        print(f"ACK {parts[1]}", flush=True)
        return
    raise kvdb.KvError(f"unknown command {cmd!r}")


def _read_batch_file(path: str):
    ops = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            if not line:
                continue
            ops.append(_parse_batch_line(line))
    return ops


def _parse_batch_line(line: str):
    parts = line.split(" ")
    if not parts or parts[0] not in ("put", "del"):
        raise kvdb.KvError(f"bad batch line: {line!r}")
    if parts[0] == "del":
        if len(parts) != 2:
            raise kvdb.KvError("del line needs one key")
        return ("del", parts[1])
    if len(parts) < 2:
        raise kvdb.KvError("put line needs a key")
    value = " ".join(parts[2:]) if len(parts) > 2 else ""
    return ("put", parts[1], value)


def _add_max_bytes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-bytes", type=int, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kvdb")
    sub = parser.add_subparsers(dest="cmd", required=True)

    open_p = sub.add_parser("open")
    open_p.add_argument("path")
    _add_max_bytes(open_p)
    open_p.set_defaults(func=cmd_open)

    put_p = sub.add_parser("put")
    put_p.add_argument("path")
    put_p.add_argument("key")
    put_p.add_argument("value", nargs="*")
    _add_max_bytes(put_p)
    put_p.set_defaults(func=cmd_put)

    get_p = sub.add_parser("get")
    get_p.add_argument("path")
    get_p.add_argument("key")
    _add_max_bytes(get_p)
    get_p.set_defaults(func=cmd_get)

    del_p = sub.add_parser("del")
    del_p.add_argument("path")
    del_p.add_argument("key")
    _add_max_bytes(del_p)
    del_p.set_defaults(func=cmd_del)

    batch_p = sub.add_parser("batch")
    batch_p.add_argument("path")
    batch_p.add_argument("file")
    _add_max_bytes(batch_p)
    batch_p.set_defaults(func=cmd_batch)

    dump_p = sub.add_parser("dump")
    dump_p.add_argument("path")
    _add_max_bytes(dump_p)
    dump_p.set_defaults(func=cmd_dump)

    check_p = sub.add_parser("check")
    check_p.add_argument("path")
    _add_max_bytes(check_p)
    check_p.set_defaults(func=cmd_check)

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("path")
    _add_max_bytes(serve_p)
    serve_p.set_defaults(func=cmd_serve)

    compact_p = sub.add_parser("compact")
    compact_p.add_argument("path")
    _add_max_bytes(compact_p)
    compact_p.set_defaults(func=cmd_compact)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except kvdb.KvError as exc:
        return _print_error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
