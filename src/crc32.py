"""High-performance, zero-allocation CRC-32 checksum using standard zlib."""

import zlib


def crc32(data: bytes) -> int:
    """Compute IEEE 802.3 32-bit unsigned CRC."""
    return zlib.crc32(data) & 0xFFFFFFFF
