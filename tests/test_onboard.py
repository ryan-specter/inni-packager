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


class FakeGitHub:
    def __init__(self):
        self.repos = {}
        self.files = {}
        self.workflows = []

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


def products():
    return manifest.group_products(manifest.parse_manifest(SAMPLE))


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
