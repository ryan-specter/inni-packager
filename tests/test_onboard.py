import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import manifest  # noqa: E402
import onboard  # noqa: E402

SAMPLE = """<slidia>
  <package name="Original Software" repo="y1-community/y1-stock-rom" device="Y1" type="img" />
  <package name="Original Software" repo="y1-community/y1-stock-rom" device="Y2" type="img" />
  <package name="Rondo" repo="jarvissax/rondo" device="Y1" type="img" />
</slidia>
"""


def release(tag, *, size=100, updated_at="2026-01-01T00:00:00Z"):
    return {
        "tag_name": tag,
        "name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [
            {
                "id": 1,
                "name": "rom.zip",
                "size": size,
                "digest": "sha256:abc",
                "updated_at": updated_at,
                "state": "uploaded",
            }
        ],
    }


class FakeGitHub:
    def __init__(self):
        self.repos = {}
        self.files = {}
        self.workflows = []
        self.releases = {}

    def get_repo(self, repo):
        return self.repos.get(repo)

    def create_repo(self, org, name, description="", private=False, auto_init=True):
        repo = {
            "full_name": f"{org}/{name}",
            "default_branch": "main",
            "archived": False,
        }
        self.repos[repo["full_name"]] = repo
        return repo

    def get_file(self, repo, path, ref=None):
        return self.files.get((repo, path))

    def put_file(self, repo, path, text, message, branch):
        key = (repo, path)
        old = self.files.get(key)
        if old and old.get("text") == text:
            return False
        self.files[key] = {"text": text, "sha": "sha-" + path}
        return True

    def dispatch_workflow(self, repo, workflow, ref, inputs=None):
        self.workflows.append((repo, workflow, ref, inputs or {}))

    def list_releases(self, repo):
        return self.releases.get(repo, [])


def products():
    return manifest.group_products(manifest.parse_manifest(SAMPLE))


COMMENTED = """<slidia>
  <!-- <package name="Original Software" repo="y1-community/g1-stock-rom" device="G1" type="img" package_name="rom_g1.zip" /> -->
</slidia>
"""


class SourceInfoTests(unittest.TestCase):
    def test_missing_source_with_no_releases_is_reported(self):
        info = onboard.source_info(FakeGitHub(), products()[0], True)
        self.assertTrue(info["source_missing"])
        self.assertIn("source repository not found", info["source_error"])

    def test_existing_source_with_no_releases_is_not_missing(self):
        fake = FakeGitHub()
        fake.repos["y1-community/y1-stock-rom"] = {
            "full_name": "y1-community/y1-stock-rom",
            "default_branch": "main",
            "archived": False,
        }
        info = onboard.source_info(fake, products()[0], True)
        self.assertFalse(info["source_missing"])
        self.assertEqual(info["source_error"], "")
        self.assertEqual(info["release_count"], 0)


class WatchSignatureTests(unittest.TestCase):
    def test_release_digest_tracks_new_tags_and_replaced_assets(self):
        first = onboard.release_digest([release("v1")])
        self.assertEqual(first, onboard.release_digest([release("v1")]))
        self.assertNotEqual(first, onboard.release_digest([release("v2")]))
        self.assertNotEqual(
            first, onboard.release_digest([release("v1", size=101)])
        )
        self.assertNotEqual(
            first,
            onboard.release_digest(
                [release("v1", updated_at="2026-01-02T00:00:00Z")]
            ),
        )

    def test_manifest_signature_ignores_the_attribute_onboarding_adds(self):
        before = manifest.parse_manifest(SAMPLE)
        after = manifest.parse_manifest(
            SAMPLE.replace('type="img"', 'type="img" portable_zips="mirror"')
        )
        self.assertEqual(
            onboard.manifest_signature(before), onboard.manifest_signature(after)
        )

    def test_manifest_signature_changes_when_a_row_changes(self):
        changed = SAMPLE.replace(
            '<package name="Rondo" repo="jarvissax/rondo" device="Y1" type="img" />',
            '<package name="Rondo" repo="jarvissax/rondo" device="Y1" type="img" post_install="https://example.com/guide" />',
        )
        self.assertNotEqual(
            onboard.manifest_signature(manifest.parse_manifest(SAMPLE)),
            onboard.manifest_signature(manifest.parse_manifest(changed)),
        )


class ManifestSyncTests(unittest.TestCase):
    def test_portable_zips_reach_commented_rows(self):
        text, rows = onboard.set_portable_zips(
            COMMENTED,
            {"y1-community/g1-stock-rom": "y1-community/g1-stock-rom-mirror"},
        )
        self.assertEqual(rows, 1)
        self.assertIn('portable_zips="y1-community/g1-stock-rom-mirror"', text)
        self.assertTrue(text.lstrip().startswith("<slidia>"))
        self.assertIn("<!--", text)
    def test_portable_zips_are_added_only_to_matching_img_rows(self):
        text, rows = onboard.set_portable_zips(
            SAMPLE,
            {
                "y1-community/y1-stock-rom": "y1-community/y1-stock-rom-mirror",
                "jarvissax/rondo": "y1-community/rondo-mirror",
            },
        )
        self.assertEqual(rows, 3)
        self.assertEqual(text.count('portable_zips="y1-community/y1-stock-rom-mirror"'), 2)
        self.assertEqual(text.count('portable_zips="y1-community/rondo-mirror"'), 1)

    def test_portable_zips_are_updated_and_idempotent(self):
        text, rows = onboard.set_portable_zips(
            SAMPLE,
            {"y1-community/y1-stock-rom": "y1-community/y1-stock-rom-mirror"},
        )
        self.assertEqual(rows, 2)
        again, rows_again = onboard.set_portable_zips(
            text, {"y1-community/y1-stock-rom": "y1-community/y1-stock-rom-mirror"}
        )
        self.assertEqual(rows_again, 0)
        self.assertEqual(again, text)

    def test_sync_site_manifest_commits_once(self):
        fake = FakeGitHub()
        fake.files[(onboard.SITE_REPO, "slidia_manifest.xml")] = {
            "text": SAMPLE,
            "sha": "manifest-sha",
        }
        report = onboard.sync_site_manifest(fake, products(), log=lambda _message: None)
        self.assertEqual(report["action"], "updated")
        self.assertEqual(report["rows"], 3)
        updated = fake.files[(onboard.SITE_REPO, "slidia_manifest.xml")]["text"]
        self.assertIn('portable_zips="y1-community/rondo-mirror"', updated)


class ApplyTests(unittest.TestCase):
    def test_creates_repo_and_seeds_managed_files(self):
        fake = FakeGitHub()
        product = products()[0]
        report = onboard.apply_product(
            fake,
            product,
            trigger=True,
            packager_repo="ryan-specter/inni-packager",
            org="y1-community",
            log=lambda _message: None,
        )
        self.assertTrue(report["created"])
        self.assertEqual(report["action"], "created")
        self.assertEqual(report["config"], "created")
        self.assertEqual(report["workflow"], "created")
        self.assertTrue(report["triggered"])
        self.assertIn("y1-community/y1-stock-rom-mirror", fake.repos)
        config = json.loads(
            fake.files[
                ("y1-community/y1-stock-rom-mirror", "mirror.config.json")
            ]["text"]
        )
        self.assertEqual(config["branch"], "main")
        self.assertIn(
            "ryan-specter/inni-packager",
            fake.files[
                ("y1-community/y1-stock-rom-mirror", ".github/workflows/mirror.yml")
            ]["text"],
        )
        self.assertEqual(
            fake.workflows,
            [("y1-community/y1-stock-rom-mirror", "mirror.yml", "main", {})],
        )

    def test_existing_config_is_kept_by_default(self):
        fake = FakeGitHub()
        product = products()[1]
        fake.repos[product.mirror_repo] = {
            "full_name": product.mirror_repo,
            "default_branch": "main",
            "archived": False,
        }
        fake.files[(product.mirror_repo, "mirror.config.json")] = {
            "text": "{\"manual\": true}\n",
            "sha": "old",
        }
        report = onboard.apply_product(
            fake,
            product,
            trigger=False,
            log=lambda _message: None,
        )
        self.assertEqual(report["config"], "kept")
        self.assertEqual(report["workflow"], "created")
        self.assertEqual(
            fake.files[(product.mirror_repo, "mirror.config.json")]["text"],
            "{\"manual\": true}\n",
        )

    def test_archived_repo_is_skipped(self):
        fake = FakeGitHub()
        product = products()[1]
        fake.repos[product.mirror_repo] = {
            "full_name": product.mirror_repo,
            "default_branch": "main",
            "archived": True,
        }
        report = onboard.apply_product(fake, product, log=lambda _message: None)
        self.assertEqual(report["action"], "skipped")
        self.assertIn("archived", report["error"])


class WatchTriggerTests(unittest.TestCase):
    def _run(self, fake, manifest_path, state_path, out, *extra):
        with mock.patch.object(onboard, "GitHub", return_value=fake):
            return onboard.main(
                [
                    "--manifest",
                    str(manifest_path),
                    "--apply",
                    "--no-site-manifest",
                    "--state",
                    str(state_path),
                    "--trigger-changed",
                    "--out",
                    str(out),
                    *extra,
                ]
            )

    def test_first_run_triggers_all_then_a_stable_run_is_quiet(self):
        fake = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.xml"
            manifest_path.write_text(SAMPLE, encoding="utf-8")
            state_path = Path(tmp) / "watch.json"
            out = Path(tmp) / "report.json"
            status = self._run(
                fake, manifest_path, state_path, out, "--no-check-releases"
            )
            self.assertEqual(status, 0)
            self.assertEqual(len(fake.workflows), 2)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["products"]), 2)
            self.assertIn("manifest", state)

            fake.workflows.clear()
            status = self._run(
                fake, manifest_path, state_path, out, "--no-check-releases"
            )
            self.assertEqual(status, 0)
            self.assertEqual(fake.workflows, [])
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(report["watch"]["first_run"])
            self.assertFalse(report["watch"]["state_written"])

    def test_source_asset_change_triggers_only_that_mirror(self):
        fake = FakeGitHub()
        fake.releases["y1-community/y1-stock-rom"] = [release("v1")]
        fake.releases["jarvissax/rondo"] = [release("r1")]
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.xml"
            manifest_path.write_text(SAMPLE, encoding="utf-8")
            state_path = Path(tmp) / "watch.json"
            out = Path(tmp) / "report.json"
            self.assertEqual(self._run(fake, manifest_path, state_path, out), 0)
            fake.workflows.clear()
            fake.releases["y1-community/y1-stock-rom"][0]["assets"][0][
                "updated_at"
            ] = "2026-02-01T00:00:00Z"
            self.assertEqual(self._run(fake, manifest_path, state_path, out), 0)
            self.assertEqual(
                fake.workflows,
                [("y1-community/y1-stock-rom-mirror", "mirror.yml", "main", {})],
            )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["products"][0]["trigger_reason"], "source")
            self.assertEqual(report["products"][1]["trigger_reason"], "")

    def test_seed_release_change_triggers_every_mirror(self):
        fake = FakeGitHub()
        seed = onboard.DEFAULT_SEED_REPOS[0]
        fake.releases[seed] = [release("seed-v1")]
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.xml"
            manifest_path.write_text(SAMPLE, encoding="utf-8")
            state_path = Path(tmp) / "watch.json"
            out = Path(tmp) / "report.json"
            self.assertEqual(
                self._run(
                    fake, manifest_path, state_path, out, "--no-check-releases"
                ),
                0,
            )
            fake.workflows.clear()
            fake.releases[seed][0]["assets"][0]["size"] = 101
            self.assertEqual(
                self._run(
                    fake, manifest_path, state_path, out, "--no-check-releases"
                ),
                0,
            )
            self.assertEqual(len(fake.workflows), 2)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                [p["trigger_reason"] for p in report["products"]],
                ["seed", "seed"],
            )

    def test_a_new_manifest_line_triggers_its_new_mirror(self):
        fake = FakeGitHub()
        added = SAMPLE.replace(
            "</slidia>",
            '  <package name="New Firmware" repo="newowner/newrom" device="Y1" type="img" />\n</slidia>',
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.xml"
            manifest_path.write_text(SAMPLE, encoding="utf-8")
            state_path = Path(tmp) / "watch.json"
            out = Path(tmp) / "report.json"
            self.assertEqual(
                self._run(
                    fake, manifest_path, state_path, out, "--no-check-releases"
                ),
                0,
            )
            fake.workflows.clear()
            manifest_path.write_text(added, encoding="utf-8")
            self.assertEqual(
                self._run(
                    fake, manifest_path, state_path, out, "--no-check-releases"
                ),
                0,
            )
            self.assertEqual(
                fake.workflows,
                [("y1-community/newrom-mirror", "mirror.yml", "main", {})],
            )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                report["products"][-1]["trigger_reason"], "manifest"
            )

    def test_a_changed_manifest_row_triggers_only_its_product(self):
        fake = FakeGitHub()
        changed = SAMPLE.replace(
            '<package name="Rondo" repo="jarvissax/rondo" device="Y1" type="img" />',
            '<package name="Rondo" repo="jarvissax/rondo" device="Y1" type="img" post_install="https://example.com/guide" />',
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.xml"
            manifest_path.write_text(SAMPLE, encoding="utf-8")
            state_path = Path(tmp) / "watch.json"
            out = Path(tmp) / "report.json"
            self.assertEqual(
                self._run(
                    fake, manifest_path, state_path, out, "--no-check-releases"
                ),
                0,
            )
            fake.workflows.clear()
            manifest_path.write_text(changed, encoding="utf-8")
            self.assertEqual(
                self._run(
                    fake, manifest_path, state_path, out, "--no-check-releases"
                ),
                0,
            )
            self.assertEqual(
                fake.workflows,
                [("y1-community/rondo-mirror", "mirror.yml", "main", {})],
            )


class MainTests(unittest.TestCase):
    def test_plan_reports_products_without_writing(self):
        fake = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.xml"
            manifest_path.write_text(SAMPLE, encoding="utf-8")
            out = Path(tmp) / "report.json"
            with mock.patch.object(onboard, "GitHub", return_value=fake):
                status = onboard.main(
                    [
                        "--manifest",
                        str(manifest_path),
                        "--no-check-releases",
                        "--out",
                        str(out),
                    ]
                )
            self.assertEqual(status, 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(report["products"]), 2)
            self.assertEqual(
                [p["action"] for p in report["products"]], ["planned", "planned"]
            )
            self.assertEqual(report["mirror_repo"] if "mirror_repo" in report else "", "")

    def test_apply_creates_each_selected_mirror(self):
        fake = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.xml"
            manifest_path.write_text(SAMPLE, encoding="utf-8")
            out = Path(tmp) / "report.json"
            with mock.patch.object(onboard, "GitHub", return_value=fake):
                status = onboard.main(
                    [
                        "--manifest",
                        str(manifest_path),
                        "--no-check-releases",
                        "--apply",
                        "--no-site-manifest",
                        "--only",
                        "rondo",
                        "--out",
                        str(out),
                    ]
                )
            self.assertEqual(status, 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(report["products"]), 1)
            self.assertEqual(report["products"][0]["source_repo"], "jarvissax/rondo")
            self.assertIn("y1-community/rondo-mirror", fake.repos)


if __name__ == "__main__":
    unittest.main()
