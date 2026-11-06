"""A small crash-safe key-value database.

The file on disk is the truth. The dictionary in memory is only a contents
page: we rebuild it by reading committed records when we open the file.

A write is not real until its commit marker has been written and os.fsync
has returned. Recovery ignores anything after the last valid commit.
"""

from __future__ import annotations

import os
import struct
import threading
import time
import zlib

MAGIC = b"KVDB"
VERSION = 2
HEADER_SIZE = 64
TXN_MAGIC = b"TXN1"
SNP_MAGIC = b"SNP1"
OP_PUT = 1
OP_DEL = 2
OP_META = 6

MAX_KEY = 1024
MAX_VALUE = 1024 * 1024
MAX_BATCH = 10_000

_HEADER = struct.Struct("<4sIIIQQQ20s")
_U32 = struct.Struct("<I")
# magic, txn id, op count, body length, crc of the body. 24 bytes.
_TXN_HEAD = struct.Struct("<4sQIII")
# magic, txn id, generation, key count, body length, crc of the body. 36 bytes.
_SNP_HEAD = struct.Struct("<4sQQQII")
_PUT_OP = struct.Struct("<BHI")
_DEL_OP = struct.Struct("<BH")
_META_OP = struct.Struct("<BQ")


class KvError(Exception):
    """Any refusal that leaves committed data untouched."""


class DiskFullError(KvError):
    """The write would pass max_bytes. Nothing from it was committed."""


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _pause_for_crash(point: str) -> None:
    """Wait so an outside test can kill us at one named line.

    The database never sets this itself. Torture sets KVDB_CRASH_AT.
    """
    if os.environ.get("KVDB_CRASH_AT") != point:
        return
    print(f"CRASH_AT {point}", flush=True)
    time.sleep(30)


def _as_bytes(data, what: str, min_len: int, max_len: int) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise KvError(f"{what} must be bytes or text")
    if not (min_len <= len(data) <= max_len):
        raise KvError(
            f"{what} length {len(data)} is outside {min_len}..{max_len}"
        )
    return data


def pack_header(generation: int, max_bytes: int, committed_offset: int) -> bytes:
    body = _HEADER.pack(
        MAGIC,
        VERSION,
        0,
        HEADER_SIZE,
        generation,
        max_bytes,
        committed_offset,
        b"\x00" * 20,
    )
    if len(body) != 60:
        raise RuntimeError("header body must be 60 bytes")
    return body + _U32.pack(_crc(body))


def parse_header(data: bytes) -> dict:
    if len(data) < HEADER_SIZE:
        raise KvError("file is shorter than the 64-byte header")
    body = data[:60]
    magic, version, _flags, header_len, generation, max_bytes, committed, _reserved = (
        _HEADER.unpack(body)
    )
    stored = _U32.unpack(data[60:64])[0]
    if magic != MAGIC:
        raise KvError("this file does not start with the KVDB magic")
    if version != VERSION:
        raise KvError(f"unsupported version {version}")
    if header_len != HEADER_SIZE:
        raise KvError("header length is not 64")
    if stored != _crc(body):
        raise KvError("header checksum does not match")
    return {
        "generation": generation,
        "max_bytes": max_bytes,
        "committed_offset": committed,
    }


def pack_transaction(txn: int, ops) -> bytes:
    """One sealed block. The checksum covers every operation, so recovery applies all or none."""
    body = _pack_ops(ops)
    return _TXN_HEAD.pack(TXN_MAGIC, txn, len(ops), len(body), _crc(body)) + body


def pack_snapshot(txn: int, generation: int, items) -> bytes:
    """A compacted file: only the live keys, with one checksum for the whole picture."""
    parts = []
    for key, value in items:
        parts.append(_PUT_OP.pack(OP_PUT, len(key), len(value)) + key + value)
    body = b"".join(parts)
    head = _SNP_HEAD.pack(SNP_MAGIC, txn, generation, len(items), len(body), _crc(body))
    return head + body


def _pack_ops(ops) -> bytes:
    parts = []
    for op in ops:
        if op[0] == "put":
            key, value = op[1], op[2]
            parts.append(_PUT_OP.pack(OP_PUT, len(key), len(value)) + key + value)
        elif op[0] == "del":
            key = op[1]
            parts.append(_DEL_OP.pack(OP_DEL, len(key)) + key)
        elif op[0] == "meta":
            parts.append(_META_OP.pack(OP_META, op[1]))
        else:
            raise KvError(f"cannot write {op[0]}")
    return b"".join(parts)


def _scan_records(data: bytes, generation: int, max_bytes: int) -> dict:
    """Rebuild the dictionary from sealed blocks only.

    A block counts when its checksum matches and its body is fully present.
    A torn tail fails that test and is left behind last_good for truncation.
    One million keys are stored as plain records inside a block, not as a
    separate frame per key, so this loop stays inside the 2 second limit.
    """
    index: dict[bytes, bytes] = {}
    last_good = HEADER_SIZE
    max_txn = 0
    size = len(data)
    pos = HEADER_SIZE
    while pos + 4 <= size:
        magic = data[pos : pos + 4]
        if magic == TXN_MAGIC:
            if pos + 24 > size:
                break
            _magic, txn, count, body_len, crc = _TXN_HEAD.unpack_from(data, pos)
            body_at = pos + 24
            body_end = body_at + body_len
            if body_end > size or _crc(data[body_at:body_end]) != crc:
                break
            updated = _apply_ops(index, data, body_at, body_end, count, max_bytes)
            if updated is None:
                break
            max_bytes = updated
            if txn > max_txn:
                max_txn = txn
            last_good = body_end
            pos = body_end
        elif magic == SNP_MAGIC:
            if pos + 36 > size:
                break
            _magic, txn, generation, count, body_len, crc = _SNP_HEAD.unpack_from(data, pos)
            body_at = pos + 36
            body_end = body_at + body_len
            if body_end > size or _crc(data[body_at:body_end]) != crc:
                break
            fresh: dict[bytes, bytes] = {}
            if _fill_puts(fresh, data, body_at, body_end, count) is None:
                break
            index = fresh
            if txn > max_txn:
                max_txn = txn
            last_good = body_end
            pos = body_end
        else:
            break
    return {
        "index": index,
        "last_good": last_good,
        "max_txn": max_txn,
        "max_bytes": max_bytes,
        "generation": generation,
    }


def _apply_ops(index, data, start, end, count, max_bytes):
    """Parse the whole body, then store it.

    Offsets are recorded first. The dictionary changes only after every
    operation in the block has a legal length, so a bad body cannot leave
    half of its keys behind.
    """
    pos = start
    seen = 0
    ops = []
    unpack_put = _PUT_OP.unpack_from
    unpack_del = _DEL_OP.unpack_from
    unpack_meta = _META_OP.unpack_from
    while seen < count:
        if pos >= end:
            return None
        kind = data[pos]
        if kind == OP_PUT:
            if pos + 7 > end:
                return None
            _kind, key_len, value_len = unpack_put(data, pos)
            key_at = pos + 7
            value_at = key_at + key_len
            next_pos = value_at + value_len
            if (
                key_len < 1
                or key_len > MAX_KEY
                or value_len > MAX_VALUE
                or next_pos > end
            ):
                return None
            ops.append((OP_PUT, key_at, value_at, next_pos))
            pos = next_pos
        elif kind == OP_DEL:
            if pos + 3 > end:
                return None
            _kind, key_len = unpack_del(data, pos)
            key_at = pos + 3
            next_pos = key_at + key_len
            if key_len < 1 or key_len > MAX_KEY or next_pos > end:
                return None
            ops.append((OP_DEL, key_at, next_pos))
            pos = next_pos
        elif kind == OP_META:
            if pos + 9 > end:
                return None
            _kind, max_bytes = unpack_meta(data, pos)
            ops.append((OP_META, max_bytes))
            pos += 9
        else:
            return None
        seen += 1
    if pos != end:
        return None
    for op in ops:
        kind = op[0]
        if kind == OP_PUT:
            index[data[op[1] : op[2]]] = data[op[2] : op[3]]
        elif kind == OP_DEL:
            index.pop(data[op[1] : op[2]], None)
        else:
            max_bytes = op[1]
    return max_bytes


def _fill_puts(index, data, start, end, count):
    pos = start
    unpack = _PUT_OP.unpack_from
    for _ in range(count):
        if pos + 7 > end:
            return None
        kind, key_len, value_len = unpack(data, pos)
        key_at = pos + 7
        value_at = key_at + key_len
        next_pos = value_at + value_len
        if (
            kind != OP_PUT
            or key_len < 1
            or key_len > MAX_KEY
            or value_len > MAX_VALUE
            or next_pos > end
        ):
            return None
        index[data[key_at:value_at]] = data[value_at:next_pos]
        pos = next_pos
    if pos != end:
        return None
    return index



def escape_bytes(data: bytes) -> str:
    """Make bytes printable on one line. dump uses this so newlines stay inside one record."""
    parts = []
    for byte in data:
        if byte == 0x5C:
            parts.append("\\\\")
        elif byte == 0x0A:
            parts.append("\\n")
        elif byte == 0x0D:
            parts.append("\\r")
        elif byte == 0x09:
            parts.append("\\t")
        elif 32 <= byte <= 126:
            parts.append(chr(byte))
        else:
            parts.append(f"\\x{byte:02x}")
    return "".join(parts)


def unescape_bytes(text: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(text):
        if text[i] != "\\":
            out.append(ord(text[i]))
            i += 1
            continue
        if i + 1 >= len(text):
            raise KvError("truncated escape in dumped value")
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
        elif nxt == "x" and i + 3 < len(text):
            out.append(int(text[i + 2 : i + 4], 16))
            i += 4
        else:
            raise KvError("unknown escape in dumped value")
    return bytes(out)


def format_record(key: bytes, value: bytes) -> str:
    return escape_bytes(key) + "\t" + escape_bytes(value)


class Database:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._file = None
        self._lock_file = None
        self._path = None
        self._index: dict[bytes, bytes] = {}
        self._next_txn = 1
        self._max_bytes = 0
        self._generation = 1
        self._closed = True
        self.recovery_ms = 0.0
        self.recovered_torn_bytes = 0

    def open(self, path: str, max_bytes: int | None = None) -> None:
        """Open or create the database and recover.

        max_bytes is the cap for this process. None means "use the cap already
        stored in the file". Passing a number stores that cap in a committed
        record when it differs from the stored cap.
        """
        with self._lock:
            if not self._closed:
                raise KvError("database is already open")
            if max_bytes is not None and max_bytes < 0:
                raise KvError("max_bytes cannot be negative")
            path = os.path.abspath(path)
            self._acquire_file_lock(path)
            try:
                self._path = path
                self._open_or_create(path, max_bytes if max_bytes is not None else 0)
                started = time.perf_counter()
                self._recover()
                self.recovery_ms = (time.perf_counter() - started) * 1000.0
                self._closed = False
                if max_bytes is not None and max_bytes != self._max_bytes:
                    self._write_transaction([("meta", max_bytes)])
            except Exception:
                self._release_handles()
                self._closed = True
                raise

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                os.fsync(self._file.fileno())
            self._release_handles()
            self._closed = True

    def get(self, key) -> bytes | None:
        key = _as_bytes(key, "key", 1, MAX_KEY)
        with self._lock:
            self._ensure_open()
            return self._index.get(key)

    def put(self, key, value) -> None:
        key = _as_bytes(key, "key", 1, MAX_KEY)
        value = _as_bytes(value, "value", 0, MAX_VALUE)
        self.batch([("put", key, value)])

    def delete(self, key) -> None:
        """Remove key. Deleting a key that is already absent is success."""
        key = _as_bytes(key, "key", 1, MAX_KEY)
        self.batch([("del", key)])

    def batch(self, ops) -> None:
        """Apply every operation, or leave the database unchanged.

        Operations run in listed order. If the same key appears twice, the
        later operation wins. That is the rule for a put and a delete of the
        same key inside one batch.
        """
        if len(ops) > MAX_BATCH:
            raise KvError(f"batch has {len(ops)} operations; the limit is {MAX_BATCH}")
        normalized = []
        for op in ops:
            if not op:
                raise KvError("empty operation in batch")
            name = op[0]
            if name == "put":
                if len(op) != 3:
                    raise KvError("put operation needs a key and a value")
                key = _as_bytes(op[1], "key", 1, MAX_KEY)
                value = _as_bytes(op[2], "value", 0, MAX_VALUE)
                normalized.append(("put", key, value))
            elif name == "del":
                if len(op) != 2:
                    raise KvError("del operation needs a key")
                key = _as_bytes(op[1], "key", 1, MAX_KEY)
                normalized.append(("del", key))
            else:
                raise KvError(f"unknown batch operation {name!r}")
        with self._lock:
            self._ensure_open()
            if not normalized:
                return
            self._write_transaction(normalized)

    def compact(self) -> None:
        """Rewrite the file so only live keys remain.

        The new file is built beside the real file and replaces it only after
        fsync. A crash before the replace leaves the old file in charge.
        """
        with self._lock:
            self._ensure_open()
            items = sorted(self._index.items())
            txn = self._next_txn
            new_generation = self._generation + 1
            blob = pack_snapshot(txn, new_generation, items)
            header = pack_header(new_generation, self._max_bytes, HEADER_SIZE + len(blob))
            new_size = len(header) + len(blob)
            if self._max_bytes and new_size > self._max_bytes:
                raise DiskFullError(
                    f"compaction needs {new_size} bytes but max_bytes is {self._max_bytes}"
                )
            tmp = self._path + ".compacting"
            with open(tmp, "wb") as handle:
                handle.write(header)
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._file = None
            # os.replace is one operating-system call. A process kill lands
            # either before it or after it. These two pauses exist so a test
            # can TerminateProcess on each side of that call. They do nothing
            # unless KVDB_CRASH_AT is set.
            _pause_for_crash("before-replace")
            try:
                os.replace(tmp, self._path)
            except Exception:
                self._file = open(self._path, "r+b")
                self._file.seek(0, os.SEEK_END)
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            _pause_for_crash("after-replace")
            self._file = open(self._path, "r+b")
            self._recover()
            self._next_txn = max(self._next_txn, txn + 1)

    def check(self) -> str:
        """Re-read the file. Return OK, or a reason. This does not hide damage by rewriting."""
        with self._lock:
            self._ensure_open()
            self._file.seek(0)
            data = self._file.read()
            try:
                header = parse_header(data[:HEADER_SIZE])
            except KvError as exc:
                return str(exc)
            scanned = _scan_records(data, header["generation"], header["max_bytes"])
            if scanned["last_good"] != len(data):
                return f"torn or invalid tail at byte {scanned['last_good']}"
            return "OK"

    def items(self):
        with self._lock:
            self._ensure_open()
            return sorted(self._index.items())

    @property
    def key_count(self) -> int:
        with self._lock:
            self._ensure_open()
            return len(self._index)

    def _ensure_open(self) -> None:
        if self._closed or self._file is None:
            raise KvError("database is not open")

    def _acquire_file_lock(self, path: str) -> None:
        import msvcrt

        lock_path = path + ".lock"
        handle = open(lock_path, "a+b")
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise KvError("database is locked by another process") from exc
        self._lock_file = handle

    def _release_handles(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._lock_file is not None:
            import msvcrt

            try:
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._lock_file.close()
            self._lock_file = None

    def _open_or_create(self, path: str, create_max_bytes: int) -> None:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            header = pack_header(1, create_max_bytes, HEADER_SIZE)
            with open(path, "wb") as handle:
                handle.write(header)
                handle.flush()
                os.fsync(handle.fileno())
        self._file = open(path, "r+b")

    def _recover(self) -> None:
        self._file.seek(0)
        data = self._file.read()
        header = parse_header(data[:HEADER_SIZE])
        self._generation = header["generation"]
        self._max_bytes = header["max_bytes"]
        self._discard_leftover_temp()
        scanned = _scan_records(data, self._generation, self._max_bytes)
        self._index = scanned["index"]
        self._generation = scanned["generation"]
        self._max_bytes = scanned["max_bytes"]
        self._next_txn = scanned["max_txn"] + 1
        last_good = scanned["last_good"]
        self.recovered_torn_bytes = len(data) - last_good
        if last_good != len(data):
            self._file.seek(last_good)
            self._file.truncate()
            self._file.flush()
            os.fsync(self._file.fileno())
        self._file.seek(0, os.SEEK_END)

    def _apply(self, index: dict[bytes, bytes], ops) -> None:
        for op in ops:
            if op[0] == "put":
                index[op[1]] = op[2]
            elif op[0] == "del":
                index.pop(op[1], None)

    def _discard_leftover_temp(self) -> None:
        tmp = self._path + ".compacting"
        if os.path.exists(tmp):
            os.remove(tmp)

    def _write_transaction(self, ops) -> None:
        txn = self._next_txn
        block = pack_transaction(txn, ops)
        self._file.seek(0, os.SEEK_END)
        checkpoint = self._file.tell()
        limit = self._max_bytes
        # Raising the cap has to be recorded even when the file is already at the
        # old cap. The new, larger cap is the limit for that one record.
        if len(ops) == 1 and ops[0][0] == "meta" and ops[0][1] > self._max_bytes:
            limit = ops[0][1]
        if limit and checkpoint + len(block) > limit:
            raise DiskFullError(
                f"write needs {len(block)} more bytes, file is {checkpoint}, max_bytes is {limit}"
            )
        try:
            self._file.write(block)
            self._file.flush()
            os.fsync(self._file.fileno())
        except Exception:
            self._file.seek(checkpoint)
            self._file.truncate()
            self._file.flush()
            os.fsync(self._file.fileno())
            raise
        self._apply(self._index, ops)
        for op in ops:
            if op[0] == "meta":
                self._max_bytes = op[1]
        self._next_txn = txn + 1
