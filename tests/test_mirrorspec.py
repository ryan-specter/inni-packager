import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = mirrorspec.load_config(str(ROOT / "mirror.config.json"))

    def test_default_platforms(self):
        keys = [p.key for p in self.cfg.platforms]
        self.assertEqual(
            keys,
            ["windows", "windows-arm64", "linux", "linux-arm64", "macos"],
        )

    def test_output_names(self):
        rom = "rom.zip"
        expected = {
            "windows": "rom-windows.zip",
            "windows-arm64": "rom-windows-arm64.zip",
            "linux": "rom-linux.zip",
            "linux-arm64": "rom-linux-arm64.zip",
            "macos": "rom-macos.zip",
        }
        for key, name in expected.items():
            self.assertEqual(
                mirrorspec.output_name(rom, self.cfg.platform(key)), name
            )
        self.assertEqual(
            mirrorspec.output_name("rom_y2.zip", self.cfg.platform("linux-arm64")),
            "rom_y2-linux-arm64.zip",
        )

    def test_firmware_selection(self):
        assets = [
            {"name": "rom.zip", "size": 10},
            {"name": "rom-windows.zip", "size": 10},
            {"name": "UpdaterCE-Windows-Portable.zip", "size": 10},
            {"name": "updater.jpg", "size": 10},
            {"name": "system.img", "size": 10},
            {"name": "com.innioasis.y1_2.1.9.apk", "size": 10},
            {"name": "source.tar.gz", "size": 10},
            {"name": "Y2-Rockbox-V2.5-Source.zip", "size": 10},
        ]
        selected = [a["name"] for a in mirrorspec.select_firmware_assets(assets, self.cfg)]
        self.assertEqual(selected, ["rom.zip"])

    def test_firmware_name_matching_is_size_independent(self):
        self.assertTrue(mirrorspec.firmware_asset_name_matches("rom.zip", self.cfg))
        self.assertFalse(
            mirrorspec.firmware_asset_name_matches("rom-windows.zip", self.cfg)
        )
        self.assertFalse(mirrorspec.firmware_asset_name_matches("updater.jpg", self.cfg))

    def test_asset_size_limit(self):
        cfg = mirrorspec.load_config(str(ROOT / "mirror.config.json"))
        cfg.max_asset_bytes = 5
        self.assertEqual(
            mirrorspec.select_firmware_assets([{"name": "rom.zip", "size": 6}], cfg), []
        )

    def test_metadata_rules(self):
        y1 = mirrorspec.resolve_metadata(self.cfg, "rom.zip")
        self.assertEqual((y1["target_model"], y1["target_type"]), ("Y1", "Type A"))
        y2 = mirrorspec.resolve_metadata(self.cfg, "rom_y2.zip")
        self.assertEqual((y2["target_model"], y2["target_type"]), ("Y2", "Stock"))
        type_b = mirrorspec.resolve_metadata(self.cfg, "rom_type_b.zip")
        self.assertEqual((type_b["target_model"], type_b["target_type"]), ("Y1", "Type B"))

    def test_ini(self):
        text = mirrorspec.fw_package_ini(
            "Original Software", "3.1.2", "Y1", "Type A", ""
        )
        self.assertIn("[Firmware]", text)
        self.assertIn("software_name = Original Software", text)
        self.assertIn("version = 3.1.2", text)
        self.assertIn("target_model = Y1", text)
        self.assertNotIn("description", text)
        self.assertTrue(text.endswith("\r\n"))

    def test_release_version_uses_title_semver(self):
        self.assertEqual(
            mirrorspec.release_version("baseline-215", "Y2Linux 1.0 Beta 9.3.7"),
            "9.3.7",
        )
        self.assertEqual(
            mirrorspec.release_version("build-20241010", "1.0.4 - Initial Release"),
            "1.0.4",
        )
        self.assertEqual(
            mirrorspec.release_version("git-tag-98", "0.6 Rockbox (ADB) (Wi-Fi)"),
            "0.6",
        )

    def test_release_version_falls_back_to_tag_then_date(self):
        self.assertEqual(
            mirrorspec.release_version("v2.5.1", "Innioasis Updater CE"),
            "2.5.1",
        )
        self.assertEqual(
            mirrorspec.release_version("20261013", "rom_y1_type_a"),
            "October 13 2026",
        )
        self.assertEqual(
            mirrorspec.release_version("20260716-0131", "rom"),
            "July 16 2026",
        )

    def test_release_version_falls_back_to_raw_tag(self):
        self.assertEqual(
            mirrorspec.release_version("baseline-215", "rom_y2.zip"),
            "baseline-215",
        )

    def test_title_prefix_is_idempotent(self):
        release = {"tag_name": "3.1.2", "name": "System Software 3.1.2"}
        title = mirrorspec.release_title(self.cfg, release)
        self.assertEqual(title, "Portable Flashing Package: System Software 3.1.2")
        again = mirrorspec.release_title(
            self.cfg,
            {"tag_name": "3.1.2", "name": title},
        )
        self.assertEqual(again, title)

    def test_title_falls_back_to_tag(self):
        self.assertEqual(
            mirrorspec.release_title(self.cfg, {"tag_name": "3.1.2", "name": ""}),
            "Portable Flashing Package: 3.1.2",
        )

    def test_notes_round_trip(self):
        release = {"tag_name": "3.1.2", "name": "System Software", "body": "Original notes."}
        block = {
            "v": mirrorspec.BUILDER_VERSION,
            "fingerprint": "abc",
            "assets": [
                {
                    "name": "rom-windows.zip",
                    "source_asset": "rom.zip",
                    "platform": "windows",
                    "size": 123,
                    "sha256": "def",
                }
            ],
            "original_assets": ["updater.jpg"],
        }
        notes = mirrorspec.render_notes(release, block)
        self.assertTrue(notes.startswith("Original notes."))
        self.assertNotIn("Portable flashing packages", notes)
        self.assertNotIn("---", notes)
        self.assertEqual(
            notes.split("<!-- portable-mirror:", 1)[0].rstrip(), "Original notes."
        )
        parsed = mirrorspec.parse_block(notes)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["fingerprint"], "abc")
        self.assertEqual(parsed["assets"], block["assets"])

    def test_fingerprint_changes_with_platforms(self):
        full = mirrorspec.config_fingerprint(self.cfg)
        subset = mirrorspec.load_config(
            str(ROOT / "mirror.config.json"), platforms=["windows"]
        )
        self.assertNotEqual(full, mirrorspec.config_fingerprint(subset))

    def test_fingerprint_ignores_unrelated_source_asset(self):
        updater = {
            "windows": {"tag": "2.5.1", "name": "w.zip", "size": 1, "id": 1},
        }
        one = {"tag_name": "1", "assets": [{"name": "rom.zip", "size": 5, "id": 9}]}
        two = {"tag_name": "1", "assets": [{"name": "rom.zip", "size": 5, "id": 10}]}
        self.assertNotEqual(
            mirrorspec.release_fingerprint(self.cfg, one, updater),
            mirrorspec.release_fingerprint(self.cfg, two, updater),
        )

    def test_fingerprint_changes_when_a_source_zip_is_replaced(self):
        updater = {
            "windows": {"tag": "2.5.1", "name": "w.zip", "size": 1, "id": 1},
        }
        one = {
            "tag_name": "1",
            "assets": [
                {"name": "rom.zip", "size": 5, "id": 9, "digest": "sha256:aaa"}
            ],
        }
        two = {
            "tag_name": "1",
            "assets": [
                {"name": "rom.zip", "size": 5, "id": 9, "digest": "sha256:bbb"}
            ],
        }
        self.assertNotEqual(
            mirrorspec.release_fingerprint(self.cfg, one, updater),
            mirrorspec.release_fingerprint(self.cfg, two, updater),
        )

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(mirrorspec.ConfigError):
            mirrorspec.load_config(
                str(ROOT / "mirror.config.json"), platforms=["amiga"]
            )

    def test_readme_defaults_to_the_mirror_landing_page(self):
        self.assertTrue(self.cfg.readme_enabled)
        self.assertEqual(self.cfg.readme_path, "README.md")
        self.assertEqual(self.cfg.readme_commit_message, "Mirror source README")

    def test_beside_dmg_and_post_install_config(self):
        raw = json.loads((ROOT / "mirror.config.json").read_text(encoding="utf-8"))
        raw["platforms"] = {
            "macos": {
                "updater_asset": "Innioasis.Updater.for.Mac.dmg",
                "suffix": "-macos.zip",
                "layout": "beside_dmg",
                "os": "macos",
                "arch": "universal",
                "updater_repo": "y1-community/innioasis-updater",
            }
        }
        raw["post_install"] = ["https://innioasis.app/y2reborn_wifibt.html"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mirror.config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            cfg = mirrorspec.load_config(str(path))
        platform = cfg.platform("macos")
        self.assertEqual(platform.layout, "beside_dmg")
        self.assertEqual(platform.updater_repo, "y1-community/innioasis-updater")
        self.assertEqual(
            cfg.post_installs, ["https://innioasis.app/y2reborn_wifibt.html"]
        )

    def test_readme_path_cannot_escape_the_repository(self):
        raw = json.loads((ROOT / "mirror.config.json").read_text(encoding="utf-8"))
        raw["source_readme"] = {"path": "../README.md"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mirror.config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(mirrorspec.ConfigError):
                mirrorspec.load_config(str(path))


if __name__ == "__main__":
    unittest.main()
