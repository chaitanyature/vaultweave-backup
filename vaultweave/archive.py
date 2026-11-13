"""Portable, streaming single-snapshot bundles. Import publishes the ref last."""

import hashlib
import json
from pathlib import Path
import os
import struct
import tempfile

from .atomic import destination, sync_dir
from .capture import overlaps
from .chunks import HEADER, MAXIMUM, IntegrityError, decode, digest
from .manifest import check, objects, validate
from .recover import stream_file

MAGIC = b"VWPACK01"
META_LIMIT = 128 * 1024 * 1024
U32 = struct.Struct(">I")


def pack(repo, name, file):
    file = destination(file)
    if overlaps(repo.root, file):
        raise ValueError("archive destination overlaps repository")
    if os.path.lexists(file):
        raise FileExistsError(file)
    key, manifest = repo.manifest(name)
    body = (repo.root / "manifests" / key).read_bytes()
    check(len(body) <= META_LIMIT, "manifest exceeds portable archive limit")
    required = sorted(objects(manifest))
    # Validate complete file digests as well as individual chunks before exporting.
    for e in manifest["entries"]:
        if e["type"] == "file":
            stream_file(repo, e)
    hashstream = hashlib.sha256()
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=file.parent, prefix=".vw-pack-", delete=False
        ) as f:
            temp = Path(f.name)

            def write(data):
                f.write(data)
                hashstream.update(data)

            write(MAGIC)
            write(U32.pack(len(body)))
            write(bytes.fromhex(key))
            write(body)
            write(U32.pack(len(required)))
            for h in required:
                with repo.object_path(h).open("rb") as obj:
                    frame = obj.read(HEADER.size + MAXIMUM + 1)
                decode(frame, h)
                write(bytes.fromhex(h))
                write(U32.pack(len(frame)))
                write(frame)
            f.write(hashstream.digest())
            f.flush()
            os.fsync(f.fileno())
        os.link(temp, file)
        sync_dir(file.parent)
    finally:
        if temp:
            temp.unlink(missing_ok=True)
    return {
        "snapshot": name,
        "manifest": key,
        "chunks": len(required),
        "bytes": file.stat().st_size,
    }


def unpack(repo, source, name):
    if repo.ref_path(name).exists():
        raise FileExistsError(f"snapshot already exists: {name}")
    checksum = hashlib.sha256()
    new = 0
    with Path(source).open("rb") as f:

        def read(n):
            data = f.read(n)
            if len(data) != n:
                raise IntegrityError("truncated archive")
            checksum.update(data)
            return data

        check(read(len(MAGIC)) == MAGIC, "invalid archive magic")
        size = U32.unpack(read(4))[0]
        check(size <= META_LIMIT, "oversized archive manifest")
        h = read(32).hex()
        body = read(size)
        check(digest(body) == h, "archive manifest checksum mismatch")
        manifest = validate(json.loads(body))
        required = sorted(objects(manifest))
        count = U32.unpack(read(4))[0]
        check(count == len(required), "incorrect archive inventory")
        for expected in required:
            check(read(32).hex() == expected, "archive object sequence mismatch")
            size = U32.unpack(read(4))[0]
            check(size <= HEADER.size + MAXIMUM, "oversized archive chunk")
            raw = decode(read(size), expected)
            key, written = repo.store_chunk(raw)
            new += bool(written)
        check(f.read(32) == checksum.digest(), "archive checksum mismatch")
        check(not f.read(1), "trailing archive bytes")
    for e in manifest["entries"]:
        if e["type"] == "file":
            stream_file(repo, e)
    key = repo.commit(name, manifest)
    return {
        "snapshot": name,
        "manifest": key,
        "chunks": len(required),
        "new_chunks": new,
    }
