"""Stable Gear boundaries and a bounded, independently verifiable object format."""

from hashlib import sha256
from struct import Struct
import zlib

MINIMUM = 16 * 1024
TARGET = 64 * 1024
MAXIMUM = 256 * 1024
MASK64 = (1 << 64) - 1
EARLY_MASK = (1 << 17) - 1
LATE_MASK = (1 << 15) - 1
GEAR = tuple(
    int.from_bytes(sha256(b"vaultweave/gear/1/" + bytes([v])).digest()[:8], "big")
    for v in range(256)
)
HEADER = Struct(">4sBI")


class IntegrityError(ValueError):
    """An object, manifest, or publication record violates the file format."""


def digest(raw):
    return sha256(raw).hexdigest()


def chunks(stream, read_size=128 * 1024):
    """Cuts are invariant to the stream's read size, including short reads."""
    if read_size < 1:
        raise ValueError("read size must be positive")
    buf = bytearray()
    state = 0
    while block := stream.read(read_size):
        for octet in block:
            buf.append(octet)
            n = len(buf)
            if n < MINIMUM:
                continue
            state = ((state << 1) + GEAR[octet]) & MASK64
            mask = EARLY_MASK if n < TARGET else LATE_MASK
            if n == MAXIMUM or not state & mask:
                yield bytes(buf)
                buf.clear()
                state = 0
    if buf:
        yield bytes(buf)


def encode(raw):
    if not 0 < len(raw) <= MAXIMUM:
        raise ValueError("invalid chunk length")
    compressed = zlib.compress(raw, 3)
    use_compression = len(compressed) < len(raw)
    return HEADER.pack(b"VW01", int(use_compression), len(raw)) + (
        compressed if use_compression else raw
    )


def decode(data, expected):
    if not HEADER.size < len(data) <= HEADER.size + MAXIMUM:
        raise IntegrityError(f"bad object frame: {expected}")
    magic, codec, length = HEADER.unpack_from(data)
    if magic != b"VW01" or codec not in (0, 1) or not 0 < length <= MAXIMUM:
        raise IntegrityError(f"invalid object header: {expected}")
    payload = data[HEADER.size :]
    if codec:
        inflater = zlib.decompressobj()
        try:
            raw = inflater.decompress(payload, length + 1)
        except zlib.error as exc:
            raise IntegrityError(f"invalid compressed chunk: {expected}") from exc
        if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise IntegrityError(f"incomplete or oversized chunk: {expected}")
    else:
        raw = payload
    if len(raw) != length or digest(raw) != expected:
        raise IntegrityError(f"chunk digest or length mismatch: {expected}")
    return raw
