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
VERSION = 1
HEADER_SIZE = 64

FRAME_MAGIC = b"FRM1"
KIND_PUT = 1
KIND_DEL = 2
KIND_COMMIT = 3
KIND_SNAP_START = 4
KIND_SNAP_END = 5
KIND_META = 6

MAX_KEY = 1024
MAX_VALUE = 1024 * 1024
MAX_BATCH = 10_000
# A frame has to be able to hold one 1 MB value, and no larger.
MAX_FRAME = MAX_VALUE + MAX_KEY + 64

_HEADER = struct.Struct("<4sIIIQQQ20s")
_FRAME_PREFIX = struct.Struct("<4sIHHQI")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_U32U32 = struct.Struct("<II")
_U64U64 = struct.Struct("<QQ")


class KvError(Exception):
    """Any refusal that leaves committed data untouched."""


class DiskFullError(KvError):
    """The write would pass max_bytes. Nothing from it was committed."""


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


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


def pack_frame(kind: int, txn: int, payload: bytes) -> bytes:
    frame_len = 24 + len(payload) + 4
    if frame_len > MAX_FRAME:
        raise KvError("record is larger than the format allows")
    prefix = _FRAME_PREFIX.pack(FRAME_MAGIC, frame_len, kind, 0, txn, len(payload))
    without_crc = prefix + payload
    return without_crc + _U32.pack(_crc(without_crc))


def try_parse_frame(data: bytes, pos: int):
    """Return a frame dict, or None if the bytes at pos are not a full valid frame.

    None means "stop here". A torn tail and a bad checksum both return None.
    We do not raise, because a crash is allowed to leave junk at the end.
    """
    remain = len(data) - pos
    if remain < 28:
        return None
    magic, frame_len, kind, _flags, txn, plen = _FRAME_PREFIX.unpack_from(data, pos)
    if magic != FRAME_MAGIC:
        return None
    if frame_len < 28 or frame_len > MAX_FRAME or plen != frame_len - 28:
        return None
    if frame_len > remain:
        return None
    end = pos + frame_len
    without_crc = data[pos : end - 4]
    stored = _U32.unpack_from(data, end - 4)[0]
    if _crc(without_crc) != stored:
        return None
    return {
        "kind": kind,
        "txn": txn,
        "payload": data[pos + 24 : end - 4],
        "end": end,
    }


def pack_put_payload(key: bytes, value: bytes) -> bytes:
    return _U32U32.pack(len(key), len(value)) + key + value


def parse_put_payload(payload: bytes):
    if len(payload) < 8:
        return None
    klen, vlen = _U32U32.unpack_from(payload, 0)
    if not (1 <= klen <= MAX_KEY) or vlen > MAX_VALUE:
        return None
    if len(payload) != 8 + klen + vlen:
        return None
    key = payload[8 : 8 + klen]
    value = payload[8 + klen :]
    return key, value


def parse_del_payload(payload: bytes):
    if len(payload) < 4:
        return None
    (klen,) = _U32.unpack_from(payload, 0)
    if not (1 <= klen <= MAX_KEY) or len(payload) != 4 + klen:
        return None
    return payload[4:]


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
            frames = [pack_frame(KIND_SNAP_START, txn, _U64.pack(len(items)))]
            for key, value in items:
                frames.append(pack_frame(KIND_PUT, txn, pack_put_payload(key, value)))
            new_generation = self._generation + 1
            frames.append(
                pack_frame(
                    KIND_SNAP_END,
                    txn,
                    _U64U64.pack(len(items), new_generation),
                )
            )
            blob = b"".join(frames)
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
            try:
                os.replace(tmp, self._path)
            except Exception:
                self._file = open(self._path, "r+b")
                self._file.seek(0, os.SEEK_END)
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
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
                parse_header(data[:HEADER_SIZE])
            except KvError as exc:
                return str(exc)
            pos = HEADER_SIZE
            last_good = HEADER_SIZE
            while pos < len(data):
                frame = try_parse_frame(data, pos)
                if frame is None:
                    return f"torn or invalid tail at byte {pos}"
                if frame["kind"] in (KIND_COMMIT, KIND_SNAP_END):
                    last_good = frame["end"]
                pos = frame["end"]
            if last_good != len(data):
                return f"file has {len(data) - last_good} bytes after the last commit"
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

        index: dict[bytes, bytes] = {}
        last_good = HEADER_SIZE
        pending = []
        pending_txn = None
        snap_puts = None
        snap_expect = None
        max_txn = 0
        pos = HEADER_SIZE

        while pos < len(data):
            frame = try_parse_frame(data, pos)
            if frame is None:
                break
            kind = frame["kind"]
            txn = frame["txn"]
            payload = frame["payload"]
            max_txn = max(max_txn, txn)

            if kind == KIND_PUT:
                parsed = parse_put_payload(payload)
                if parsed is None:
                    break
                if snap_puts is not None:
                    snap_puts.append(parsed)
                else:
                    if pending_txn is None:
                        pending_txn = txn
                    if txn != pending_txn:
                        break
                    pending.append(("put", parsed[0], parsed[1]))
            elif kind == KIND_DEL:
                if snap_puts is not None:
                    break
                key = parse_del_payload(payload)
                if key is None:
                    break
                if pending_txn is None:
                    pending_txn = txn
                if txn != pending_txn:
                    break
                pending.append(("del", key))
            elif kind == KIND_META:
                if snap_puts is not None or len(payload) != 8:
                    break
                (new_cap,) = _U64.unpack(payload)
                if pending_txn is None:
                    pending_txn = txn
                if txn != pending_txn:
                    break
                pending.append(("meta", new_cap))
            elif kind == KIND_COMMIT:
                if snap_puts is not None or len(payload) != 4:
                    break
                (count,) = _U32.unpack(payload)
                if pending_txn != txn or count != len(pending):
                    break
                self._apply(index, pending)
                for op in pending:
                    if op[0] == "meta":
                        self._max_bytes = op[1]
                pending = []
                pending_txn = None
                last_good = frame["end"]
            elif kind == KIND_SNAP_START:
                if pending or snap_puts is not None or len(payload) != 8:
                    break
                (snap_expect,) = _U64.unpack(payload)
                snap_puts = []
                pending_txn = txn
            elif kind == KIND_SNAP_END:
                if snap_puts is None or len(payload) != 16:
                    break
                count, generation = _U64U64.unpack(payload)
                if count != len(snap_puts) or count != snap_expect or txn != pending_txn:
                    break
                index = {key: value for key, value in snap_puts}
                self._generation = generation
                snap_puts = None
                snap_expect = None
                pending_txn = None
                last_good = frame["end"]
            else:
                break
            pos = frame["end"]

        self._index = index
        self._next_txn = max_txn + 1
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
        frames = []
        for op in ops:
            if op[0] == "put":
                payload = pack_put_payload(op[1], op[2])
                frames.append(pack_frame(KIND_PUT, txn, payload))
            elif op[0] == "del":
                payload = _U32.pack(len(op[1])) + op[1]
                frames.append(pack_frame(KIND_DEL, txn, payload))
            elif op[0] == "meta":
                frames.append(pack_frame(KIND_META, txn, _U64.pack(op[1])))
            else:
                raise KvError(f"cannot write {op[0]}")
        commit = pack_frame(KIND_COMMIT, txn, _U32.pack(len(ops)))
        body = b"".join(frames)
        self._file.seek(0, os.SEEK_END)
        checkpoint = self._file.tell()
        extra = len(body) + len(commit)
        limit = self._max_bytes
        # Raising the cap has to be recorded even when the file is already at the
        # old cap. The new, larger cap is the limit for that one record.
        if len(ops) == 1 and ops[0][0] == "meta" and ops[0][1] > self._max_bytes:
            limit = ops[0][1]
        if limit and checkpoint + extra > limit:
            raise DiskFullError(
                f"write needs {extra} more bytes, file is {checkpoint}, max_bytes is {limit}"
            )
        try:
            self._file.write(body)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.write(commit)
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
