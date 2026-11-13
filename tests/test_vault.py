import io
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from vaultweave.atomic import rename_new
from vaultweave.capture import Changed, capture
from vaultweave.chunks import (
    HEADER,
    MAXIMUM,
    IntegrityError,
    chunks,
    decode,
    digest,
    encode,
)
from vaultweave.recover import diff, restore, verify
from vaultweave.repository import Repository, initialize

ROOT = Path(__file__).resolve().parents[1]


class ChunkTests(unittest.TestCase):
    def test_boundaries_ignore_read_buffer_and_reassemble(self):
        raw = random.Random(72).randbytes(700000)
        expected = list(chunks(io.BytesIO(raw)))
        for n in (13, 4096, 524288):
            self.assertEqual(list(chunks(io.BytesIO(raw), n)), expected)
        self.assertEqual(b"".join(expected), raw)
        self.assertLessEqual(max(map(len, expected)), MAXIMUM)
        self.assertEqual(list(chunks(io.BytesIO(b""))), [])

    def test_stored_and_deflated_corruption(self):
        for raw in [b"A" * 20000, random.Random(1).randbytes(5000)]:
            frame = encode(raw)
            self.assertEqual(decode(frame, digest(raw)), raw)
            for bad in [b"", frame[:-1], frame + b"tail", b"bad!" + frame[4:]]:
                with self.assertRaises(IntegrityError):
                    decode(bad, digest(raw))
        with self.assertRaises(IntegrityError):
            decode(
                HEADER.pack(b"VW01", 1, 1) + zlib.compress(b"x" * 1000000), digest(b"x")
            )


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "src"
        self.source.mkdir()
        self.repo = Repository(initialize(self.base / "vault"))

    def backup(self, name="v1", parent=None):
        with self.repo.session(True):
            return capture(self.repo, self.source, name, parent)

    def test_restore_preserves_unusual_names_links_empty_bytes_and_mode(self):
        raw = self.source / "☀️ \n.txt"
        raw.write_bytes(b"")
        os.chmod(raw, 0o640)
        (self.source / "empty").mkdir()
        (self.source / "soft").symlink_to("../missing")
        os.link(raw, self.source / "hard")
        self.backup()
        where = self.base / "dest"
        restore(self.repo, "v1", where)
        a = where / raw.name
        self.assertEqual(a.read_bytes(), b"")
        self.assertEqual(a.stat().st_mode & 0o777, 0o640)
        self.assertEqual(a.stat().st_mtime_ns, raw.stat().st_mtime_ns)
        self.assertTrue(a.samefile(where / "hard"))
        self.assertEqual(os.readlink(where / "soft"), "../missing")
        self.assertTrue((where / "empty").is_dir())
        with self.assertRaises(FileExistsError):
            restore(self.repo, "v1", where)

    def test_incremental_reuses_only_identical_source_signatures(self):
        f = self.source / "f"
        f.write_text("green")
        (self.source / "constant").write_bytes(b"x" * 40000)
        self.backup()
        t = f.stat().st_mtime_ns
        f.write_text("amber")
        os.utime(f, ns=(t, t))
        self.assertEqual(self.backup("v2", "v1")["files_read"], 1)
        self.assertIn({"change": "content", "path": "f"}, diff(self.repo, "v1", "v2"))
        self.assertEqual(self.backup("v3", "v2")["bytes_read"], 0)
        with self.assertRaises(FileExistsError):
            self.backup("v2")
        other = self.base / "another"
        other.mkdir()
        with self.assertRaises(ValueError):
            capture(self.repo, other, "bad", "v1")

    def test_invalid_topology_is_rejected_even_for_matching_manifest_digest(self):
        self.backup()
        manifest = self.repo.manifest("v1")[1]
        for entries in [
            [
                {"type": "symlink", "path": "escape", "target": "/tmp"},
                {"type": "dir", "path": "escape/child", "mode": 0o700, "mtime_ns": 0},
            ],
            [{"type": "symlink", "path": "../oops", "target": "x"}],
            [{"type": "hardlink", "path": "alias", "target": "../outside"}],
            [
                {"type": "symlink", "path": "repeat", "target": "x"},
                {"type": "symlink", "path": "repeat", "target": "z"},
            ],
        ]:
            unsafe = {**manifest, "entries": [manifest["entries"][0], *entries]}
            body = json.dumps(unsafe).encode()
            key = digest(body)
            (self.repo.root / "manifests" / key).write_bytes(body)
            self.repo.ref_path("v1").write_text(key)
            with self.assertRaises(IntegrityError):
                restore(self.repo, "v1", self.base / "out")
            self.assertFalse((self.base / "out").exists())

    def test_corrupt_object_blocks_verified_restore_and_existing_content_reuse(self):
        (self.source / "a").write_bytes(b"hello world" * 400)
        self.backup()
        stored = next((self.repo.root / "objects").glob("*/*"))
        stored.write_bytes(stored.read_bytes()[:-1])
        for action in [
            lambda: verify(self.repo),
            lambda: restore(self.repo, "v1", self.base / "dest"),
            lambda: self.backup("v2"),
        ]:
            with self.assertRaises(IntegrityError):
                action()
        self.assertFalse((self.base / "dest").exists())
        self.assertFalse(list(self.base.glob(".vw-restore-*")))
        self.assertEqual(self.repo.names(), ["v1"])

    def test_digest_verified_after_all_chunks(self):
        (self.source / "x").write_bytes(b"v" * 8000)
        self.backup()
        m = self.repo.manifest("v1")[1]
        m["entries"][1]["sha256"] = "f" * 64
        self.repo.commit("forged", m)
        with self.assertRaisesRegex(IntegrityError, "whole-file"):
            restore(self.repo, "forged", self.base / "restored")
        self.assertFalse((self.base / "restored").exists())

    def test_exclusive_publish_cannot_replace_even_empty_destination_directory(self):
        a = self.base / "ready"
        b = self.base / "target"
        a.mkdir()
        b.mkdir()
        with self.assertRaises(FileExistsError):
            rename_new(a, b)
        self.assertTrue(a.exists())

    def test_rejects_special_files_overlap_and_name_traversal(self):
        os.mkfifo(self.source / "fifo")
        with self.assertRaisesRegex(ValueError, "special file"):
            self.backup()
        with self.assertRaisesRegex(ValueError, "disjoint"):
            capture(self.repo, self.base, "unsafe")
        with self.assertRaises(ValueError):
            self.repo.ref_path("../outside")
        self.assertEqual(self.repo.names(), [])

    def test_mid_read_edit_never_publishes_mixed_file(self):
        f = self.source / "large"
        f.write_bytes(b"Z" * 500000)
        orig = self.repo.store_chunk
        changed = False

        def mutate(raw):
            nonlocal changed
            if not changed:
                changed = True
                with f.open("ab") as out:
                    out.write(b"alter")
            return orig(raw)

        with patch.object(self.repo, "store_chunk", side_effect=mutate):
            with self.assertRaises(Changed):
                self.backup()
        self.assertEqual(self.repo.names(), [])

    def test_gc_refuses_sweep_with_a_missing_live_object(self):
        (self.source / "a").write_text("keep")
        self.backup()
        key, _ = self.repo.store_chunk(b"orphan")
        root_key = self.repo.manifest("v1")[1]["entries"][1]["chunks"][0][0]
        self.repo.object_path(root_key).unlink()
        with self.assertRaisesRegex(IntegrityError, "missing live"):
            self.repo.collect(True)
        self.assertTrue(self.repo.object_path(key).exists())

    def test_killed_writer_releases_lock_and_leaves_only_reclaimable_chunks(self):
        (self.source / "stable").write_bytes(b"keep")
        self.backup()
        (self.source / "new").write_bytes(random.Random(383).randbytes(900000))
        program = """
import os,signal,sys
from vaultweave.repository import Repository
from vaultweave.capture import capture
repo=Repository(sys.argv[1])
def die_before_ref(name,manifest):
    os.kill(os.getpid(),signal.SIGKILL)
repo.commit=die_before_ref
with repo.session(True):capture(repo,sys.argv[2],"aborted","v1")
"""
        run = subprocess.run(
            [sys.executable, "-c", program, self.repo.root, self.source], cwd=ROOT
        )
        self.assertEqual(run.returncode, -signal.SIGKILL)
        with self.repo.session(True):
            self.assertEqual(self.repo.names(), ["v1"])
            self.assertGreater(self.repo.collect()["chunks"], 0)
            self.repo.collect(True)
            self.assertEqual(self.repo.collect()["chunks"], 0)
            self.assertEqual(verify(self.repo)["verified_bytes"], 4)

    def test_shared_session_blocks_gc_until_reader_exits(self):
        (self.source / "x").write_text("abc")
        self.backup()
        # Two synchronization pipes: parent knows the child is about to take the lock;
        # first try an actual nonblocking exclusive lock, then run the blocking writer.
        code = """
import fcntl,sys,os,json
from vaultweave.repository import Repository
r=Repository(sys.argv[1])
with open(r.root/"LOCK","rb") as f:
    try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:print("blocked",flush=True)
    else:raise AssertionError("unexpected exclusive lock")
with r.session(True):print(json.dumps(r.collect(True)),flush=True)
"""
        worker = None
        try:
            with self.repo.session(False):
                worker = subprocess.Popen(
                    [sys.executable, "-c", code, self.repo.root],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                )
                self.assertEqual(worker.stdout.readline().strip(), "blocked")
                self.assertEqual(verify(self.repo)["snapshots"], 1)
            out, _ = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0)
            self.assertTrue(json.loads(out)["apply"])
        finally:
            if worker and worker.poll() is None:
                worker.kill()
                worker.wait()


if __name__ == "__main__":
    unittest.main()
