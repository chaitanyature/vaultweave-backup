"""Validate all path topology before opening any restore destination for extraction."""

import json
import re

from .chunks import IntegrityError, MAXIMUM

HEX = re.compile(r"[0-9a-f]{64}\Z")
LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z")


def json_bytes(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def check(ok, message):
    if not ok:
        raise IntegrityError(message)


def label(value):
    if not isinstance(value, str) or not LABEL.fullmatch(value):
        raise ValueError("snapshot name must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}")
    return value


def valid_hash(value):
    return isinstance(value, str) and bool(HEX.fullmatch(value))


def integer(value):
    return type(value) is int


def by_path(entries):
    return {e["path"]: e for e in entries}


def validate(manifest):
    check(
        isinstance(manifest, dict) and manifest.get("version") == 1,
        "unknown manifest version",
    )
    source = manifest.get("source")
    check(
        isinstance(source, list)
        and len(source) == 2
        and all(integer(i) and i >= 0 for i in source),
        "invalid source identity",
    )
    entries = manifest.get("entries")
    check(isinstance(entries, list) and bool(entries), "empty manifest")
    seen = {}
    for i, e in enumerate(entries):
        check(isinstance(e, dict), "invalid entry")
        p = e.get("path")
        check(isinstance(p, str) and "\x00" not in p, "invalid entry path")
        parts = p.split("/") if p else []
        check(
            all(part not in ("", ".", "..") for part in parts), "unsafe path component"
        )
        check(p not in seen, "duplicate path")
        kind = e.get("type")
        check(kind in ("dir", "file", "symlink", "hardlink"), "unknown entry type")
        if i == 0:
            check(p == "" and kind == "dir", "invalid root entry")
        else:
            check(
                bool(p) and seen.get(p.rpartition("/")[0], {}).get("type") == "dir",
                "ancestor missing or not a directory",
            )
        if kind in ("file", "dir"):
            check(
                integer(e.get("mode")) and 0 <= e["mode"] <= 0o777,
                "invalid permission mode",
            )
            check(integer(e.get("mtime_ns")), "invalid mtime")
        if kind == "file":
            size = e.get("size")
            check(
                integer(size) and size >= 0 and valid_hash(e.get("sha256")),
                "invalid file descriptor",
            )
            sig = e.get("signature")
            check(
                isinstance(sig, list)
                and len(sig) == 3
                and all(integer(k) for k in sig),
                "invalid change signature",
            )
            refs = e.get("chunks")
            check(isinstance(refs, list), "invalid chunk list")
            total = 0
            for ref in refs:
                check(
                    isinstance(ref, list)
                    and len(ref) == 2
                    and valid_hash(ref[0])
                    and integer(ref[1])
                    and 0 < ref[1] <= MAXIMUM,
                    "invalid chunk reference",
                )
                total += ref[1]
            check(total == size, "chunk lengths do not cover file")
        elif kind == "hardlink":
            target = e.get("target")
            check(
                isinstance(target, str) and seen.get(target, {}).get("type") == "file",
                "invalid hardlink target",
            )
        elif kind == "symlink":
            target = e.get("target")
            check(
                isinstance(target, str) and target and "\x00" not in target,
                "invalid symbolic link",
            )
        seen[p] = e
    return manifest


def objects(manifest):
    return {
        h for e in manifest["entries"] if e["type"] == "file" for h, _ in e["chunks"]
    }
