"""Repository lifecycle, public snapshot catalog and immutable content objects."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from .atomic import destination, lock_repository, rename_new, sync_dir
from .chunks import HEADER, MAXIMUM, IntegrityError, decode, digest, encode
from .manifest import check, json_bytes, label, objects, valid_hash, validate

FORMAT = b'{"format":"vaultweave","version":1}\n'


def initialize(path):
    root = destination(path)
    staging = Path(tempfile.mkdtemp(dir=root.parent, prefix=".vw-init-"))
    try:
        for folder in ("objects", "manifests", "refs", "tmp"):
            (staging / folder).mkdir(mode=0o700)
        for name, content in [("LOCK", b""), ("FORMAT", FORMAT)]:
            with (staging / name).open("xb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        sync_dir(staging)
        rename_new(staging, root)
        sync_dir(root.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return root


class Repository:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if (self.root / "FORMAT").read_bytes() != FORMAT:
            raise IntegrityError("unrecognized repository format")

    @contextmanager
    def session(self, write=False):
        with lock_repository(self.root, write):
            yield self

    def publish(self, target, data):
        """Target is either a fresh path or an identical immutable object."""
        temporary = self.root / "tmp" / ("write-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.link(temporary, target)
            sync_dir(target.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def ref_path(self, name):
        return self.root / "refs" / label(name)

    def names(self):
        return sorted(label(p.name) for p in (self.root / "refs").iterdir())

    def manifest(self, name):
        key = self.ref_path(name).read_text("ascii")
        check(valid_hash(key), f"invalid snapshot reference: {name}")
        data = (self.root / "manifests" / key).read_bytes()
        check(digest(data) == key, f"manifest checksum mismatch: {name}")
        return key, validate(json.loads(data))

    def object_path(self, key):
        check(valid_hash(key), "invalid chunk ID")
        return self.root / "objects" / key[:2] / key[2:]

    def read_chunk(self, key):
        with self.object_path(key).open("rb") as f:
            raw = f.read(MAXIMUM + HEADER.size + 1)
        return decode(raw, key)

    def store_chunk(self, raw):
        key = digest(raw)
        path = self.object_path(key)
        if path.exists():
            # Detect a damaged existing object before deduplicating a fresh scan to it.
            self.read_chunk(key)
            return key, 0
        if not path.parent.is_dir():
            path.parent.mkdir()
            sync_dir(path.parent.parent)
        blob = encode(raw)
        self.publish(path, blob)
        return key, len(blob)

    def commit(self, name, manifest):
        data = json_bytes(validate(manifest))
        key = digest(data)
        path = self.root / "manifests" / key
        if path.exists():
            check(path.read_bytes() == data, "conflicting manifest object")
        else:
            self.publish(path, data)
        self.publish(self.ref_path(name), key.encode("ascii"))
        return key

    def forget(self, name):
        self.ref_path(name).unlink()
        sync_dir(self.root / "refs")

    def collect(self, apply=False):
        """Validate every live root and required chunk's existence before sweeping."""
        live = set()
        manifests = set()
        for name in self.names():
            key, manifest = self.manifest(name)
            manifests.add(key)
            live.update(objects(manifest))
        for h in live:
            check(self.object_path(h).is_file(), f"missing live object: {h}")
        garbage = []
        for folder in (self.root / "objects").iterdir():
            if (
                len(folder.name) != 2
                or any(c not in "0123456789abcdef" for c in folder.name)
                or not folder.is_dir()
            ):
                raise IntegrityError(f"unexpected object shard {folder}")
            for path in folder.iterdir():
                key = folder.name + path.name
                check(valid_hash(key) and path.is_file(), "invalid object path")
                if key not in live:
                    garbage.append(path)
        obsolete = []
        for path in (self.root / "manifests").iterdir():
            check(valid_hash(path.name) and path.is_file(), "invalid manifest path")
            if path.name not in manifests:
                obsolete.append(path)
        temp = list((self.root / "tmp").glob("write-*"))
        victims = garbage + obsolete + temp
        result = {
            "apply": apply,
            "chunks": len(garbage),
            "manifests": len(obsolete),
            "temporary_files": len(temp),
            "bytes": sum(p.stat().st_size for p in victims),
        }
        if apply:
            for p in victims:
                p.unlink()
            for parent in {p.parent for p in victims}:
                sync_dir(parent)
        return result
