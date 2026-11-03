import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402
import portable_mirror as pm  # noqa: E402


def config():
    cfg = mirrorspec.load_config(
        str(ROOT / "mirror.config.json"), mirror_repo="owner/mirror", environ={}
    )
    cfg.copy_original_assets = True
    return cfg


def asset(name, size, digest):
    return {"name": name, "size": size, "digest": digest}


def source_release(assets, tag="1.0"):
    return {"tag_name": tag, "name": "1.0", "body": "", "assets": assets}


class ReviewReleaseTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.updater = {}

    def block_for(self, release, names):
        return {
            "v": pm.BUILDER_VERSION,
            "fingerprint": pm.release_fingerprint(self.cfg, release, self.updater),
            "failures": [],
            "assets": [{"name": name, "size": 10} for name in names],
        }

    def review(self, release, existing, block):
        with mock.patch.object(pm, "parse_block", return_value=block):
            return pm.review_release(self.cfg, release, existing, self.updater)

    def test_missing_release(self):
        release = source_release([asset("rom.zip", 5, "sha256:aaa")])
        item = self.review(release, None, None)
        self.assertEqual(item["state"], "missing-release")

    def test_current_release(self):
        release = source_release([asset("rom.zip", 5, "sha256:aaa")])
        existing = {
            "assets": [
                asset("rom.zip", 5, "sha256:aaa"),
                asset("rom-windows.zip", 10, "sha256:bbb"),
            ]
        }
        item = self.review(
            release, existing, self.block_for(release, ["rom-windows.zip"])
        )
        self.assertEqual(item["state"], "current")
        self.assertEqual(item["missing"], [])
        self.assertEqual(item["changed"], [])

    def test_changed_original_asset(self):
        release = source_release([asset("rom.zip", 5, "sha256:new")])
        existing = {
            "assets": [
                asset("rom.zip", 5, "sha256:old"),
                asset("rom-windows.zip", 10, "sha256:bbb"),
            ]
        }
        item = self.review(
            release, existing, self.block_for(release, ["rom-windows.zip"])
        )
        self.assertEqual(item["state"], "changed-assets")
        self.assertEqual(item["changed"], ["rom.zip"])

    def test_missing_original_asset(self):
        release = source_release(
            [asset("rom.zip", 5, "sha256:aaa"), asset("update.zip", 7, "sha256:ccc")]
        )
        existing = {
            "assets": [
                asset("rom.zip", 5, "sha256:aaa"),
                asset("rom-windows.zip", 10, "sha256:bbb"),
            ]
        }
        item = self.review(
            release, existing, self.block_for(release, ["rom-windows.zip"])
        )
        self.assertEqual(item["state"], "missing-assets")
        self.assertEqual(item["missing"], ["update.zip"])

    def test_new_updater_fingerprint_needs_a_build(self):
        release = source_release([asset("rom.zip", 5, "sha256:aaa")])
        existing = {
            "assets": [
                asset("rom.zip", 5, "sha256:aaa"),
                asset("rom-windows.zip", 10, "sha256:bbb"),
            ]
        }
        block = self.block_for(release, ["rom-windows.zip"])
        block["fingerprint"] = "stale"
        item = self.review(release, existing, block)
        self.assertEqual(item["state"], "needs-build")

    def test_missing_package_needs_a_build(self):
        release = source_release([asset("rom.zip", 5, "sha256:aaa")])
        existing = {"assets": [asset("rom.zip", 5, "sha256:aaa")]}
        item = self.review(
            release, existing, self.block_for(release, ["rom-windows.zip"])
        )
        self.assertEqual(item["state"], "needs-build")


if __name__ == "__main__":
    unittest.main()
