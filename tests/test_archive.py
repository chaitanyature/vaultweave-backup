import tempfile
import unittest
from pathlib import Path

from vaultweave.archive import pack, unpack
from vaultweave.capture import capture
from vaultweave.chunks import IntegrityError
from vaultweave.recover import restore, verify
from vaultweave.repository import Repository, initialize


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "live"
        self.src.mkdir()
        (self.src / "sample").write_bytes(bytes(range(256)) * 1200)
        (self.src / "link").symlink_to("sample")
        self.repo = Repository(initialize(self.root / "primary"))
        self.copy = Repository(initialize(self.root / "replica"))
        capture(self.repo, self.src, "base")
        self.arc = self.root / "transit.vwp"
        self.summary = pack(self.repo, "base", self.arc)

    def test_import_deduplicates_across_names_and_restores_into_fresh_repository(self):
        imported = unpack(self.copy, self.arc, "replicated")
        self.assertEqual(imported["manifest"], self.summary["manifest"])
        self.assertGreater(imported["new_chunks"], 0)
        self.assertEqual(unpack(self.copy, self.arc, "second-name")["new_chunks"], 0)
        out = self.root / "restored"
        restore(self.copy, "replicated", out)
        self.assertEqual(
            (out / "link").read_bytes(), (self.src / "sample").read_bytes()
        )
        with self.assertRaises(FileExistsError):
            unpack(self.copy, self.arc, "replicated")
        self.assertEqual(verify(self.copy)["snapshots"], 2)

    def test_transport_corruption_and_truncation_leave_no_published_reference(self):
        original = self.arc.read_bytes()
        for bad in (
            original[:5],
            original[:-7],
            original + b"x",
            original[:-1] + bytes([original[-1] ^ 1]),
            original[:8] + b"\xff" * 4 + original[12:],
        ):
            self.arc.write_bytes(bad)
            with self.assertRaises(IntegrityError):
                unpack(self.copy, self.arc, "bad")
            self.assertEqual(self.copy.names(), [])
        self.assertGreater(self.copy.collect(True)["chunks"], 0)
        self.assertEqual(self.copy.collect(True)["bytes"], 0)

    def test_pack_preserves_existing_files(self):
        with self.assertRaises(FileExistsError):
            pack(self.repo, "base", self.arc)
        with self.assertRaises(ValueError):
            pack(self.repo, "base", self.repo.root / "bad")


if __name__ == "__main__":
    unittest.main()
