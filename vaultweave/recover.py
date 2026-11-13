"""Verify complete file hashes. Publish a restore only after all bytes check out."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from .atomic import destination, rename_new, sync_dir
from .capture import overlaps
from .manifest import by_path, check


def stream_file(repo, entry, sink=None):
    checksum = hashlib.sha256()
    for key, length in entry["chunks"]:
        raw = repo.read_chunk(key)
        check(len(raw) == length, f"wrong chunk size for {entry['path']}")
        checksum.update(raw)
        if sink is not None:
            sink.write(raw)
    check(
        checksum.hexdigest() == entry["sha256"],
        f"whole-file checksum mismatch: {entry['path']}",
    )


def verify(repo, name=None):
    names = [name] if name else repo.names()
    files = logical = refs = 0
    unique = set()
    for snapshot in names:
        _, m = repo.manifest(snapshot)
        for e in m["entries"]:
            if e["type"] == "file":
                stream_file(repo, e)
                files += 1
                logical += e["size"]
                refs += len(e["chunks"])
                unique.update(h for h, _ in e["chunks"])
    return {
        "snapshots": len(names),
        "regular_files": files,
        "verified_bytes": logical,
        "chunk_references": refs,
        "unique_chunks": len(unique),
    }


def clean_stage(stage):
    if not stage.exists():
        return
    # Metadata finalization may already have applied read-only modes.
    os.chmod(stage, 0o700)
    for root, dirs, _ in os.walk(stage, topdown=True, followlinks=False):
        os.chmod(root, 0o700)
        for name in dirs:
            path = Path(root) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)
    shutil.rmtree(stage)


def restore(repo, name, target):
    target = destination(target)
    if overlaps(repo.root, target):
        raise ValueError("restore destination overlaps repository")
    if os.path.lexists(target):
        raise FileExistsError(target)
    _, manifest = repo.manifest(name)
    stage = Path(tempfile.mkdtemp(dir=target.parent, prefix=".vw-restore-"))
    files = 0
    try:
        dirs = []
        for e in manifest["entries"]:
            path = stage / e["path"]
            kind = e["type"]
            if kind == "dir":
                if e["path"]:
                    path.mkdir(mode=0o700)
                dirs.append((path, e))
            elif kind == "file":
                with path.open("xb") as f:
                    stream_file(repo, e, f)
                    f.flush()
                    os.fchmod(f.fileno(), e["mode"])
                    os.utime(f.fileno(), ns=(e["mtime_ns"], e["mtime_ns"]))
                    os.fsync(f.fileno())
                files += 1
            elif kind == "hardlink":
                os.link(stage / e["target"], path, follow_symlinks=False)
            else:
                os.symlink(e["target"], path)
        for path, e in reversed(dirs):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(fd, e["mode"])
                os.utime(fd, ns=(e["mtime_ns"], e["mtime_ns"]))
                os.fsync(fd)
            finally:
                os.close(fd)
        rename_new(stage, target)
        sync_dir(target.parent)
    finally:
        clean_stage(stage)
    return {"snapshot": name, "destination": str(target), "regular_files": files}


def diff(repo, first, second):
    """Separate filesystem-visible changes from metadata used only for reuse."""
    a = by_path(repo.manifest(first)[1]["entries"])
    b = by_path(repo.manifest(second)[1]["entries"])
    result = []
    for p in sorted(a.keys() | b.keys()):
        if p not in a:
            change = "added"
        elif p not in b:
            change = "removed"
        else:
            x, y = a[p], b[p]
            if x["type"] != y["type"]:
                change = "type"
            elif (x.get("sha256"), x.get("target")) != (
                y.get("sha256"),
                y.get("target"),
            ):
                change = "content"
            elif (x.get("mode"), x.get("mtime_ns")) != (
                y.get("mode"),
                y.get("mtime_ns"),
            ):
                change = "metadata"
            else:
                continue
        result.append({"path": p, "change": change})
    return result
