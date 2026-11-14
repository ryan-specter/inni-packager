import argparse
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402
import portable_mirror as pm  # noqa: E402
import release_notes  # noqa: E402

DIR_MODE = (0o40755 << 16) | 0x10
FILE_MODE = 0o100644 << 16


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = DIR_MODE if name.endswith("/") else mode
            archive.writestr(info, data)


def asset(name, size=1, ident=1):
    return {
        "name": name,
        "size": size,
        "id": ident,
        "browser_download_url": "https://example.invalid/" + name,
    }


class FakeGitHub:
    def __init__(self, releases):
        self.releases = releases

    def list_releases(self, repo):
        return self.releases


class BuildForReleaseTests(unittest.TestCase):
    def test_original_zip_is_repacked_and_recorded(self):
        cfg = mirrorspec.load_config(
            str(ROOT / "mirror.config.json"),
            mirror_repo="owner/mirror",
            platforms=["windows"],
            environ={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            firmware = work / "rom.zip"
            make_zip(
                firmware,
                [
                    ("MT6572_Android_scatter.txt", b"standard", FILE_MODE),
                    ("preserve_data_scatter.txt", b"stray", FILE_MODE),
                    ("boot.img", b"firmware payload " * 2000, FILE_MODE),
                ],
            )
            tool = work / "UpdaterCE-Windows-Portable.zip"
            make_zip(
                tool,
                [
                    ("SP_Flash_Tool/", b"", DIR_MODE),
                    ("SP_Flash_Tool/flash_tool.exe", b"tool", FILE_MODE),
                    ("SP_Flash_Tool/MTK_AllInOne_DA.bin", b"da", FILE_MODE),
                    ("SP_Flash_Tool/history.ini", b"[old]\n", FILE_MODE),
                    ("InnioasisUpdater", b"app", FILE_MODE),
                ],
            )
            release = {
                "tag_name": "1.0",
                "name": "Test 1.0",
                "body": "notes",
                "assets": [
                    {
                        "name": "rom.zip",
                        "size": firmware.stat().st_size,
                        "id": 1,
                        "browser_download_url": "https://example.invalid/rom.zip",
                    }
                ],
            }

            class FakeGH:
                def download(self, repo, asset, dest, log=None):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(firmware.read_bytes())
                    return dest

            updater = {
                "windows": {
                    "repo": "owner/updater",
                    "tag": "2.0",
                    "name": tool.name,
                    "size": tool.stat().st_size,
                    "id": 2,
                    "url": "https://example.invalid/tool.zip",
                }
            }
            result = pm.build_for_release(
                cfg,
                release,
                None,
                None,
                FakeGH(),
                updater,
                None,
                lambda key: tool,
                work,
                quiet,
                dry_run=True,
            )
            self.assertEqual(result["action"], "built-dry-run")
            original = work / "1.0" / "original" / "out" / "rom.zip"
            self.assertTrue(original.is_file())
            with zipfile.ZipFile(original) as archive:
                names = archive.namelist()
            self.assertIn("MT6572_Android_scatter.txt", names)
            self.assertNotIn("preserve_data_scatter.txt", names)
            self.assertLessEqual(original.stat().st_size, firmware.stat().st_size)
            block = result["block"]
            self.assertEqual(block["original_assets"], ["rom.zip"])
            self.assertEqual(
                block["original_asset_sizes"]["rom.zip"],
                original.stat().st_size,
            )


class AssetLimitTests(unittest.TestCase):
    def test_oversized_platform_is_dropped_not_fatal(self):
        """One variant over the per-asset limit must not sink the release."""

        cfg = mirrorspec.load_config(
            str(ROOT / "mirror.config.json"),
            mirror_repo="owner/mirror",
            platforms=["windows", "linux"],
            environ={},
        )
        # The input firmware must fit; only the generated package is oversized.
        cfg.max_asset_bytes = 1_000_000
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            firmware = work / "rom.zip"
            make_zip(
                firmware,
                [
                    ("MT6572_Android_scatter.txt", b"standard", FILE_MODE),
                    ("boot.img", b"firmware payload " * 400, FILE_MODE),
                ],
            )
            tool = work / "UpdaterCE-Windows-Portable.zip"
            make_zip(
                tool,
                [
                    ("SP_Flash_Tool/", b"", DIR_MODE),
                    ("SP_Flash_Tool/flash_tool.exe", b"tool", FILE_MODE),
                    ("SP_Flash_Tool/MTK_AllInOne_DA.bin", b"da", FILE_MODE),
                    ("InnioasisUpdater", b"app", FILE_MODE),
                ],
            )
            release = {
                "tag_name": "1.0",
                "name": "Test 1.0",
                "body": "notes",
                "assets": [
                    {
                        "name": "rom.zip",
                        "size": firmware.stat().st_size,
                        "id": 1,
                        "browser_download_url": "https://example.invalid/rom.zip",
                    }
                ],
            }

            class FakeGH:
                def download(self, repo, asset, dest, log=None):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(firmware.read_bytes())
                    return dest

            updater = {
                key: {
                    "repo": "owner/updater",
                    "tag": "2.0",
                    "name": tool.name,
                    "size": tool.stat().st_size,
                    "id": 2,
                    "url": "https://example.invalid/tool.zip",
                }
                for key in ("windows", "linux")
            }
            real_build = pm.build_portable_package

            def oversized_linux(*args, **kwargs):
                result = real_build(*args, **kwargs)
                platform = kwargs.get("platform") or args[2]
                if getattr(platform, "key", "") == "linux":
                    result.size = cfg.max_asset_bytes + 1
                return result

            from unittest import mock

            with mock.patch.object(pm, "build_portable_package", oversized_linux):
                result = pm.build_for_release(
                    cfg,
                    release,
                    None,
                    None,
                    FakeGH(),
                    updater,
                    None,
                    lambda key: tool,
                    work,
                    quiet,
                    dry_run=True,
                )
            self.assertEqual(result["action"], "built-dry-run")
            self.assertEqual(len(result["assets"]), 1)
            self.assertTrue(result["assets"][0].endswith("-windows.zip"))
            self.assertTrue(
                any("asset limit" in failure for failure in result["failures"])
            )


def quiet(_message):
    return None


class UpdaterSelectionTests(unittest.TestCase):
    def cfg(self, platforms=None):
        return mirrorspec.load_config(
            str(ROOT / "mirror.config.json"),
            mirror_repo="owner/mirror",
            platforms=platforms,
        )

    def test_newest_release_with_each_asset_wins(self):
        releases = [
            {
                "tag_name": "2.6",
                "assets": [asset("UpdaterCE-Windows-Portable.zip", 10, 1)],
            },
            {
                "tag_name": "2.5.1",
                "assets": [
                    asset("UpdaterCE-Linux-Portable.zip", 20, 2),
                    asset("Innioasis.Updater.for.Mac.dmg", 30, 3),
                ],
            },
        ]
        updater = pm.resolve_updater_assets(
            self.cfg(["windows", "linux", "macos"]),
            FakeGitHub(releases),
            quiet,
        )
        self.assertEqual(updater["windows"]["tag"], "2.6")
        self.assertEqual(updater["linux"]["tag"], "2.5.1")
        self.assertEqual(updater["macos"]["tag"], "2.5.1")

    def test_arm64_is_built_when_the_asset_appears(self):
        releases = [
            {
                "tag_name": "2.5.1",
                "assets": [
                    asset("UpdaterCE-Linux-Portable.zip", 20, 2),
                    asset("UpdaterCE-Linux-Portable-arm64.zip", 21, 4),
                ],
            }
        ]
        updater = pm.resolve_updater_assets(
            self.cfg(["linux", "linux-arm64"]),
            FakeGitHub(releases),
            quiet,
        )
        self.assertIn("linux-arm64", updater)
        self.assertEqual(updater["linux-arm64"]["name"], "UpdaterCE-Linux-Portable-arm64.zip")

    def test_doubled_zip_suffix_still_matches_the_newest_app(self):
        releases = [
            {
                "tag_name": "2.5.4",
                "assets": [asset("UpdaterCE-Windows-Portable.zip.zip", 40, 9)],
            },
            {
                "tag_name": "2.5.1",
                "assets": [asset("UpdaterCE-Windows-Portable.zip", 10, 1)],
            },
        ]
        updater = pm.resolve_updater_assets(
            self.cfg(["windows"]), FakeGitHub(releases), quiet
        )
        self.assertEqual(updater["windows"]["tag"], "2.5.4")
        self.assertEqual(
            updater["windows"]["name"], "UpdaterCE-Windows-Portable.zip.zip"
        )

    def test_missing_arm64_is_optional(self):
        releases = [
            {
                "tag_name": "2.5.1",
                "assets": [asset("UpdaterCE-Linux-Portable.zip", 20, 2)],
            }
        ]
        updater = pm.resolve_updater_assets(
            self.cfg(["linux", "linux-arm64"]),
            FakeGitHub(releases),
            quiet,
        )
        self.assertEqual(list(updater), ["linux"])

    def test_missing_required_asset_aborts(self):
        releases = [
            {
                "tag_name": "2.5.1",
                "assets": [asset("UpdaterCE-Linux-Portable.zip", 20, 2)],
            }
        ]
        with self.assertRaises(SystemExit):
            pm.resolve_updater_assets(
                self.cfg(["windows", "linux"]),
                FakeGitHub(releases),
                quiet,
            )


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.cfg = mirrorspec.load_config(
            str(ROOT / "mirror.config.json"), mirror_repo="owner/mirror"
        )

    def test_block_is_current(self):
        block = {
            "v": mirrorspec.BUILDER_VERSION,
            "fingerprint": "fp",
            "assets": [{"name": "rom-windows.zip", "size": 10}],
            "button_assets": [{"name": "release-button-updater.svg", "size": 10}],
        }
        existing = {
            "body": "notes",
            "assets": [
                {"name": "rom-windows.zip", "size": 10, "state": "uploaded"},
                {
                    "name": "release-button-updater.svg",
                    "size": 10,
                    "state": "uploaded",
                },
            ],
        }
        release = {"tag_name": "3.1.2"}
        self.assertTrue(
            pm.block_is_current(self.cfg, release, existing, block, "fp")
        )
        self.assertFalse(
            pm.block_is_current(self.cfg, release, existing, block, "other")
        )
        existing["assets"][0]["size"] = 11
        self.assertFalse(
            pm.block_is_current(self.cfg, release, existing, block, "fp")
        )

    def test_block_is_current_checks_original_assets(self):
        block = {
            "v": mirrorspec.BUILDER_VERSION,
            "fingerprint": "fp",
            "assets": [{"name": "rom-windows.zip", "size": 10}],
            "button_assets": [{"name": "release-button-updater.svg", "size": 10}],
            "original_assets": ["rom.zip"],
            "original_asset_sizes": {"rom.zip": 6},
        }
        existing = {
            "body": "notes",
            "assets": [
                {"name": "rom-windows.zip", "size": 10, "state": "uploaded"},
                {
                    "name": "release-button-updater.svg",
                    "size": 10,
                    "state": "uploaded",
                },
                {"name": "rom.zip", "size": 6, "state": "uploaded"},
            ],
        }
        release = {"tag_name": "3.1.2"}
        self.assertTrue(
            pm.block_is_current(self.cfg, release, existing, block, "fp")
        )
        existing["assets"][2]["size"] = 7
        self.assertFalse(
            pm.block_is_current(self.cfg, release, existing, block, "fp")
        )
        existing["assets"].pop()
        self.assertFalse(
            pm.block_is_current(self.cfg, release, existing, block, "fp")
        )

    def test_index_entry_url_and_title(self):
        block = {
            "assets": [
                {
                    "name": "rom_type_b-windows.zip",
                    "source_asset": "rom_type_b.zip",
                    "platform": "windows",
                    "size": 42,
                    "sha256": "abc",
                }
            ],
            "original_assets": ["rom_type_b.zip"],
        }
        release = {
            "tag_name": "type-b-1.7.6",
            "name": "System Software Update 1.7.6",
            "published_at": "2026-01-01T00:00:00Z",
            "body": "## Source notes\n\n- item",
        }
        entry = pm.build_index_entry(self.cfg, release, block)
        self.assertEqual(
            entry["title"],
            "Portable Flashing Package: System Software Update 1.7.6",
        )
        self.assertEqual(entry["source_title"], "System Software Update 1.7.6")
        self.assertEqual(entry["source_notes"], "## Source notes\n\n- item")
        self.assertEqual(
            entry["assets"][0]["url"],
            "https://github.com/owner/mirror/releases/download/"
            "type-b-1.7.6/rom_type_b-windows.zip",
        )
        self.assertEqual(entry["assets"][0]["source_asset"], "rom_type_b.zip")

    def test_stats_url_is_indexed(self):
        block = {"assets": [], "original_assets": []}
        release = {"tag_name": "3.1.2", "name": "System Software"}
        entry = pm.build_index_entry(self.cfg, release, block)
        self.assertEqual(
            entry["stats_url"],
            "https://github.com/owner/mirror/releases/download/3.1.2/stats.txt",
        )

    def test_fingerprint_changes_when_updater_updates(self):
        release = {
            "tag_name": "3.1.2",
            "assets": [{"name": "rom.zip", "size": 1, "id": 1}],
        }
        first = {
            "windows": {"tag": "2.5.1", "name": "w.zip", "size": 1, "id": 1}
        }
        second = {
            "windows": {"tag": "2.6", "name": "w.zip", "size": 2, "id": 2}
        }
        self.assertNotEqual(
            mirrorspec.release_fingerprint(self.cfg, release, first),
            mirrorspec.release_fingerprint(self.cfg, release, second),
        )

    def test_fingerprint_changes_when_a_firmware_asset_updates(self):
        updater = {
            "windows": {"tag": "2.6", "name": "w.zip", "size": 1, "id": 1}
        }
        first = {
            "tag_name": "3.1.2",
            "assets": [{"name": "rom.zip", "size": 1, "id": 1}],
        }
        second = {
            "tag_name": "3.1.2",
            "assets": [{"name": "rom.zip", "size": 2, "id": 2}],
        }
        self.assertNotEqual(
            mirrorspec.release_fingerprint(self.cfg, first, updater),
            mirrorspec.release_fingerprint(self.cfg, second, updater),
        )

    def test_review_uses_recorded_original_size(self):
        release = {
            "tag_name": "3.1.2",
            "assets": [
                {
                    "name": "rom.zip",
                    "size": 100,
                    "id": 1,
                    "digest": "sha256:source-bytes",
                }
            ],
        }
        updater = {
            "windows": {
                "tag": "2.6",
                "name": "UpdaterCE-Windows-Portable.zip",
                "size": 10,
                "id": 2,
            }
        }
        block = {
            "v": mirrorspec.BUILDER_VERSION,
            "fingerprint": mirrorspec.release_fingerprint(
                self.cfg, release, updater
            ),
            "assets": [{"name": "rom-windows.zip", "size": 42}],
            "button_assets": [{"name": "release-button-updater.svg", "size": 1}],
            "original_assets": ["rom.zip"],
            "original_asset_sizes": {"rom.zip": 60},
        }
        existing = {
            "body": mirrorspec.render_notes(release, block),
            "assets": [
                {"name": "rom-windows.zip", "size": 42, "state": "uploaded"},
                {
                    "name": "release-button-updater.svg",
                    "size": 1,
                    "state": "uploaded",
                },
                {"name": "rom.zip", "size": 60, "state": "uploaded"},
            ],
        }
        self.assertEqual(
            pm.review_release(self.cfg, release, existing, updater)["state"],
            "current",
        )
        existing["assets"][2]["size"] = 61
        self.assertEqual(
            pm.review_release(self.cfg, release, existing, updater)["state"],
            "needs-build",
        )

    def test_make_index_merges_and_removes(self):
        previous = {
            "schema": 1,
            "releases": [
                {"tag": "old", "published_at": "2020-01-01T00:00:00Z"},
                {"tag": "stale", "published_at": "2019-01-01T00:00:00Z"},
            ],
        }
        updates = {
            "new": {"tag": "new", "published_at": "2026-01-01T00:00:00Z"},
            "stale": None,
        }
        index = pm.make_index(self.cfg, updates, previous)
        tags = [entry["tag"] for entry in index["releases"]]
        self.assertEqual(tags, ["new", "old"])
        self.assertEqual(index["mirror_repo"], "owner/mirror")


class FakeReadmeGitHub:
    def __init__(self, text="# Source README\n", changed=True):
        self.text = text
        self.changed = changed
        self.calls = []

    def get_readme(self, repo):
        return {
            "name": "README.md",
            "path": "README.md",
            "text": self.text,
            "html_url": "https://github.com/source/repo/blob/main/README.md",
        }

    def put_file(self, repo, path, text, message, branch):
        self.calls.append(
            {
                "repo": repo,
                "path": path,
                "text": text,
                "message": message,
                "branch": branch,
            }
        )
        return self.changed


class FakeIndexGitHub:
    def __init__(self, changed=True):
        self.changed = changed
        self.calls = []

    def put_file(self, repo, path, text, message, branch):
        self.calls.append(
            {"repo": repo, "path": path, "text": text, "branch": branch}
        )
        return self.changed


class IndexPublishTests(unittest.TestCase):
    def cfg(self):
        return mirrorspec.load_config(
            str(ROOT / "mirror.config.json"), mirror_repo="owner/mirror"
        )

    def test_index_is_published_through_the_contents_api(self):
        fake = FakeIndexGitHub()
        index = {"schema": 1, "releases": [{"tag": "v1"}]}
        changed = pm.publish_index(self.cfg(), fake, index, quiet)
        self.assertTrue(changed)
        self.assertEqual(fake.calls[0]["repo"], "owner/mirror")
        self.assertEqual(fake.calls[0]["path"], "index.json")
        self.assertEqual(fake.calls[0]["branch"], "main")
        self.assertEqual(json.loads(fake.calls[0]["text"]), index)
        self.assertTrue(fake.calls[0]["text"].endswith("\n"))

    def test_unchanged_index_is_reported_but_not_an_error(self):
        fake = FakeIndexGitHub(changed=False)
        self.assertFalse(pm.publish_index(self.cfg(), fake, {}, quiet))
        self.assertEqual(len(fake.calls), 1)

    def test_publish_requires_a_mirror_repo(self):
        # Actions sets GITHUB_REPOSITORY, which load_config treats as a default
        # mirror; pass an empty environment to test the unset case.
        cfg = mirrorspec.load_config(str(ROOT / "mirror.config.json"), environ={})
        fake = FakeIndexGitHub()
        self.assertFalse(pm.publish_index(cfg, fake, {}, quiet))
        self.assertEqual(fake.calls, [])


class ReadmeSyncTests(unittest.TestCase):
    def cfg(self):
        return mirrorspec.load_config(
            str(ROOT / "mirror.config.json"), mirror_repo="owner/mirror"
        )

    def test_source_readme_is_copied_and_uploaded(self):
        fake = FakeReadmeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            info = pm.sync_source_readme(
                self.cfg(), fake, Path(tmp), quiet, dry_run=False
            )
            self.assertEqual(info["action"], "updated")
            self.assertEqual(info["path"], "README.md")
            self.assertEqual(fake.calls[0]["repo"], "owner/mirror")
            self.assertEqual(fake.calls[0]["branch"], "main")
            self.assertEqual(fake.calls[0]["text"], "# Source README\n")
            self.assertEqual(
                (Path(tmp) / "readme" / "README.md").read_text(encoding="utf-8"),
                "# Source README\n",
            )

    def test_dry_run_fetches_without_writing_to_github(self):
        fake = FakeReadmeGitHub()
        fake.put_file = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
        with tempfile.TemporaryDirectory() as tmp:
            info = pm.sync_source_readme(
                self.cfg(), fake, Path(tmp), quiet, dry_run=True
            )
        self.assertEqual(info["action"], "dry-run")

    def test_missing_source_readme_is_not_a_failure(self):
        fake = FakeReadmeGitHub()
        fake.get_readme = lambda repo: None
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                pm.sync_source_readme(self.cfg(), fake, Path(tmp), quiet, dry_run=False)
            )

    def test_source_readme_can_be_disabled(self):
        cfg = self.cfg()
        cfg.readme_enabled = False
        fake = FakeReadmeGitHub()
        fake.get_readme = lambda repo: (_ for _ in ()).throw(AssertionError())
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                pm.sync_source_readme(cfg, fake, Path(tmp), quiet, dry_run=False)
            )

    def test_index_records_readme_without_run_status(self):
        index = pm.make_index(
            self.cfg(),
            {},
            None,
            {
                "path": "README.md",
                "source_path": "README.md",
                "source_url": "https://github.com/source/repo/blob/main/README.md",
                "url": "https://raw.githubusercontent.com/owner/mirror/main/README.md",
                "action": "updated",
            },
        )
        self.assertNotIn("action", index["source_readme"])
        self.assertEqual(index["source_readme"]["path"], "README.md")


class ShardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = mirrorspec.load_config(ROOT / "mirror.config.json")

    def test_parse_shard_accepts_a_slice(self):
        self.assertEqual(pm.parse_shard("2/5"), (2, 5))
        self.assertIsNone(pm.parse_shard(""))

    def test_parse_shard_rejects_bad_values(self):
        with self.assertRaises(mirrorspec.ConfigError):
            pm.parse_shard("5/5")
        with self.assertRaises(mirrorspec.ConfigError):
            pm.parse_shard("nonsense")

    def test_shards_cover_every_release_once(self):
        releases = [{"tag_name": f"t{index}"} for index in range(11)]
        seen = []
        for index in range(3):
            class Args:
                tags = ""
                limit = None
                offset = 0
                shard = f"{index}/3"

            picked = pm.filter_source_releases(self.cfg, releases, Args())
            seen.extend(release["tag_name"] for release in picked)
        self.assertEqual(len(seen), len(releases))
        self.assertEqual(set(seen), {release["tag_name"] for release in releases})


class MirrorSourceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = mirrorspec.load_config(ROOT / "mirror.config.json")

    def mirror_release(self):
        block = {
            "v": mirrorspec.BUILDER_VERSION,
            "fingerprint": "old",
            "assets": [
                {
                    "name": "rom-linux.zip",
                    "platform": "linux",
                    "source_asset": "rom.zip",
                    "model": "Y1",
                    "type": "Type A",
                    "size": 10,
                    "sha256": "x",
                }
            ],
            "button_assets": [{"name": "release-button-linux.svg", "size": 1}],
            "original_assets": ["rom.zip", "notes.txt"],
            "original_asset_sizes": {"rom.zip": 5, "notes.txt": 2},
            "stats": {"baseline": {}, "retired": {}},
            "failures": [],
        }
        source = {
            "tag_name": "20260101",
            "name": "Original 20260101",
            "body": "Original notes for this build.\n",
        }
        body = mirrorspec.render_notes(
            source,
            block,
            header=release_notes.render_header(self.cfg, source, block),
        )
        return {
            "tag_name": "20260101",
            "name": self.cfg.title_prefix + "Original 20260101",
            "body": body,
            "published_at": "2026-01-01T00:00:00Z",
            "prerelease": False,
            "assets": [
                asset("rom.zip", 5, 1),
                asset("rom-linux.zip", 10, 2),
                asset("notes.txt", 2, 3),
            ],
        }

    def test_strip_generated_header_returns_source_notes(self):
        notes = release_notes.strip_generated_header(self.mirror_release()["body"])
        self.assertIn("Original notes for this build.", notes)
        self.assertNotIn("release-button", notes)
        self.assertNotIn("portable-mirror", notes)

    def test_mirror_source_keeps_only_stored_originals(self):
        picked = pm.mirror_source_releases(
            self.cfg, FakeGitHub([self.mirror_release()]), pm.Log(False)
        )
        self.assertEqual(len(picked), 1)
        self.assertEqual(
            [item["name"] for item in picked[0]["assets"]],
            ["rom.zip", "notes.txt"],
        )
        self.assertEqual(picked[0]["name"], "Original 20260101")

    def test_rebuild_index_lists_published_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            args = argparse.Namespace(index=str(path), publish_index=False)
            gh = FakeGitHub([self.mirror_release()])
            self.assertEqual(pm.rebuild_index(self.cfg, gh, args, pm.Log(False)), 0)
            index = json.loads(path.read_text())
        entry = index["releases"][0]
        self.assertEqual(entry["tag"], "20260101")
        self.assertEqual([item["name"] for item in entry["assets"]], ["rom-linux.zip"])


if __name__ == "__main__":
    unittest.main()
