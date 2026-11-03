import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402
import portable_mirror as pm  # noqa: E402


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
                    asset("Innioasis.Updater.for.Mac.zip", 30, 3),
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
        }
        existing = {
            "body": "notes",
            "assets": [{"name": "rom-windows.zip", "size": 10, "state": "uploaded"}],
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
        cfg = mirrorspec.load_config(str(ROOT / "mirror.config.json"))
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


if __name__ == "__main__":
    unittest.main()
