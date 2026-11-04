import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import source_sync  # noqa: E402


class SourceSyncTests(unittest.TestCase):
    def test_copies_source_without_source_automation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "mirror"
            (source / ".github" / "workflows").mkdir(parents=True)
            (source / "pkg").mkdir()
            (source / "README.md").write_text("# Source\n", encoding="utf-8")
            (source / "pkg" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (source / ".github" / "workflows" / "build.yml").write_text("on: push\n", encoding="utf-8")
            (source / ".github" / "CODEOWNERS").write_text("* @owner\n", encoding="utf-8")
            target.mkdir()
            (target / "mirror.config.json").write_text('{"source_repo": "a/b"}\n', encoding="utf-8")
            (target / "index.json").write_text('{"schema": 1}\n', encoding="utf-8")
            (target / ".github" / "workflows").mkdir(parents=True)
            (target / ".github" / "workflows" / "mirror.yml").write_text("name: mirror\n", encoding="utf-8")
            (target / "stale.txt").write_text("old\n", encoding="utf-8")
            (target / "README.md").write_text("# Old\n", encoding="utf-8")
            (target / ".mirror-assets").mkdir()
            (target / ".mirror-assets" / "release-button-updater.svg").write_text(
                "<svg/>\n", encoding="utf-8"
            )

            result = source_sync.sync_source(source, target, "a/b")

            self.assertEqual(result["copied"], 2)
            self.assertGreaterEqual(result["removed"], 1)
            self.assertTrue(
                (target / ".mirror-assets" / "release-button-updater.svg").exists()
            )
            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "# Source\n")
            self.assertEqual((target / "pkg" / "app.py").read_text(encoding="utf-8"), "print('ok')\n")
            self.assertFalse((target / ".github" / "workflows" / "build.yml").exists())
            self.assertFalse((target / ".github" / "CODEOWNERS").exists())
            self.assertTrue((target / ".github" / "workflows" / "mirror.yml").exists())
            self.assertTrue((target / "mirror.config.json").exists())
            self.assertTrue((target / "index.json").exists())
            self.assertFalse((target / "stale.txt").exists())

    def test_generates_a_readme_when_source_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "mirror"
            source.mkdir()
            target.mkdir()
            (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (target / "README.md").write_text("# Old\n", encoding="utf-8")

            result = source_sync.sync_source(source, target, "a/b")

            self.assertTrue(result["generated_readme"])
            readme = (target / "README.md").read_text(encoding="utf-8")
            self.assertIn("https://github.com/a/b", readme)
            self.assertIn("Portable flashing packages", readme)

    def test_handles_lowercase_source_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "mirror"
            source.mkdir()
            target.mkdir()
            (source / "readme.md").write_text("# Lower\n", encoding="utf-8")
            (target / "README.md").write_text("# Old\n", encoding="utf-8")

            result = source_sync.sync_source(source, target, "a/b")

            self.assertEqual(result["readme"], ["readme.md"])
            self.assertEqual((target / "readme.md").read_text(encoding="utf-8"), "# Lower\n")


if __name__ == "__main__":
    unittest.main()
