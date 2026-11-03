import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gh import GitHub  # noqa: E402


class UploadTests(unittest.TestCase):
    def test_unchanged_asset_is_skipped(self):
        gh = GitHub(token="")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rom.zip"
            path.write_bytes(b"firmware")
            digest = "sha256:" + hashlib.sha256(b"firmware").hexdigest()
            gh.get_release_by_tag = lambda repo, tag: {
                "assets": [{"name": "rom.zip", "size": 8, "digest": digest}]
            }
            gh._run_gh = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("unchanged asset was uploaded")
            )
            stats = gh.upload("owner/mirror", "1.0", [path])
        self.assertEqual(stats, {"uploaded": 0, "skipped": 1, "bytes": 0})

    def test_changed_asset_is_uploaded_in_place(self):
        gh = GitHub(token="")
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rom.zip"
            path.write_bytes(b"new firmware")
            gh.get_release_by_tag = lambda repo, tag: {
                "assets": [
                    {"name": "rom.zip", "size": 3, "digest": "sha256:old"},
                    {"name": "stats.txt", "size": 1, "digest": "sha256:keep"},
                ]
            }
            gh._run_gh = lambda args, **kwargs: calls.append(args)
            stats = gh.upload("owner/mirror", "1.0", [path])
        self.assertEqual(stats["uploaded"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(calls[0][:4], ["release", "upload", "1.0", "--repo"])
        self.assertIn("--clobber", calls[0])
        self.assertEqual(calls[0][-1], str(path))

    def test_asset_without_digest_falls_back_to_size(self):
        gh = GitHub(token="")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rom.zip"
            path.write_bytes(b"same size")
            gh.get_release_by_tag = lambda repo, tag: {
                "assets": [{"name": "rom.zip", "size": 9}]
            }
            gh._run_gh = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("size-equal asset was uploaded")
            )
            stats = gh.upload("owner/mirror", "1.0", [path])
        self.assertEqual(stats["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
