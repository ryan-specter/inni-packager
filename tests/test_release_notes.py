import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402
import release_notes  # noqa: E402


class ReleaseNoteTests(unittest.TestCase):
    def setUp(self):
        self.cfg = mirrorspec.load_config(
            str(ROOT / "mirror.config.json"), mirror_repo="owner/mirror"
        )
        self.release = {
            "tag_name": "baseline-215",
            "name": "Y2Linux 1.0 Beta 9.3.7",
            "body": "## Source notes\n\nOriginal text.",
        }
        self.block = {
            "assets": [
                {
                    "name": "rom-windows.zip",
                    "source_asset": "rom.zip",
                    "platform": "windows",
                    "model": "Y1",
                    "type": "Type A",
                    "size": 100,
                },
                {
                    "name": "rom-macos.zip",
                    "source_asset": "rom.zip",
                    "platform": "macos",
                    "model": "Y1",
                    "type": "Type A",
                    "size": 101,
                },
                {
                    "name": "rom_y2-linux.zip",
                    "source_asset": "rom_y2.zip",
                    "platform": "linux",
                    "model": "Y2",
                    "type": "Stock",
                    "size": 102,
                },
            ],
        }

    def test_button_assets_are_self_contained_svgs(self):
        assets = release_notes.button_assets(["windows", "macos", "linux"])
        self.assertEqual(
            set(assets),
            {
                "release-button-updater.svg",
                "release-button-windows.svg",
                "release-button-macos.svg",
                "release-button-linux.svg",
            },
        )
        self.assertIn(b"Get it on Updater CE", assets["release-button-updater.svg"])
        self.assertIn(b"macOS 12+", assets["release-button-macos.svg"])
        self.assertIn(b"<svg", assets["release-button-windows.svg"])

    def test_header_groups_downloads_by_model_and_type(self):
        header = release_notes.render_header(
            self.cfg, self.release, self.block
        )
        self.assertIn('align="center"', header)
        self.assertIn("### Downloads", header)
        self.assertIn("#### Y1 · Type A", header)
        self.assertIn("#### Y2 · Stock", header)
        self.assertIn("release-button-windows.svg", header)
        self.assertIn("release-button-macos.svg", header)
        self.assertIn("release-button-linux.svg", header)
        self.assertIn(
            "https://raw.githubusercontent.com/owner/mirror/main/.mirror-assets/"
            "release-button-windows.svg",
            header,
        )
        self.assertIn(
            "https://github.com/owner/mirror/releases/download/"
            "baseline-215/rom-windows.zip",
            header,
        )
        self.assertIn("https://innioasis.app/guide.html?", header)
        self.assertIn("repo=y1-community%2Fy1-stock-rom", header)
        self.assertIn("tag=baseline-215", header)
        self.assertIn("#download-updater", header)

    def test_header_starts_notes_and_preserves_source_body(self):
        header = release_notes.render_header(self.cfg, self.release, self.block)
        notes = mirrorspec.render_notes(self.release, self.block, header=header)
        self.assertTrue(notes.startswith('<p align="center">'))
        self.assertIn("## Source notes", notes)
        self.assertTrue(notes.rstrip().endswith("-->"))
        self.assertLess(notes.index('<p align="center">'), notes.index("## Source notes"))

    def test_button_url_is_served_from_the_mirror_branch(self):
        self.assertEqual(
            release_notes.button_url(self.cfg, "release-button-updater.svg"),
            "https://raw.githubusercontent.com/owner/mirror/main/.mirror-assets/"
            "release-button-updater.svg",
        )

    def test_button_files_can_be_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = release_notes.write_button_assets(tmp, ["linux-arm64"])
            names = {path.name for path in paths}
            self.assertEqual(
                names,
                {"release-button-updater.svg", "release-button-linux-arm64.svg"},
            )
            self.assertTrue(all(path.is_file() for path in paths))


if __name__ == "__main__":
    unittest.main()
