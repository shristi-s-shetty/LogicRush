"""Constants and configuration for KVDB storage engine."""

MAGIC = b"KVDB"
VERSION = 2
HEADER_SIZE = 64
TXN_MAGIC = b"TXN1"
SNP_MAGIC = b"SNP1"

OP_PUT = 1
OP_DEL = 2
OP_META = 6

# IEEE 802.3 CRC32 header and data limits
MAX_KEY = 1024
MAX_VALUE = 1024 * 1024  # 1 MB
MAX_BATCH = 10_000
