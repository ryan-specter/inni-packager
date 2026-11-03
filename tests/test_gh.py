import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gh import GitHub  # noqa: E402


class RepositoryFileTests(unittest.TestCase):
    def test_file_payload_decodes_readme_content(self):
        payload = GitHub._file_payload(
            {
                "name": "README.md",
                "path": "README.md",
                "sha": "abc",
                "content": base64.b64encode(b"# Firmware\n").decode("ascii"),
                "html_url": "https://github.com/o/r/blob/main/README.md",
            }
        )
        self.assertEqual(payload["text"], "# Firmware\n")
        self.assertEqual(payload["sha"], "abc")
        self.assertEqual(payload["name"], "README.md")

    def test_put_file_skips_an_identical_file(self):
        gh = GitHub(token="")
        gh.get_file = lambda repo, path, ref=None: {
            "sha": "abc",
            "text": "# Firmware\n",
        }
        gh.request_json = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
        self.assertFalse(
            gh.put_file("owner/mirror", "README.md", "# Firmware\n", "msg", "main")
        )

    def test_put_file_sends_content_and_existing_sha(self):
        gh = GitHub(token="")
        gh.get_file = lambda repo, path, ref=None: {
            "sha": "abc",
            "text": "old",
        }
        captured = {}

        def request_json(path, params=None, method="GET", data=None):
            captured.update(path=path, method=method, data=data)
            return {}

        gh.request_json = request_json
        self.assertTrue(
            gh.put_file("owner/mirror", "README.md", "# New\n", "msg", "main")
        )
        self.assertEqual(captured["method"], "PUT")
        self.assertEqual(captured["path"], "/repos/owner/mirror/contents/README.md")
        self.assertEqual(captured["data"]["sha"], "abc")
        self.assertEqual(captured["data"]["branch"], "main")
        self.assertEqual(
            base64.b64decode(captured["data"]["content"]).decode("utf-8"), "# New\n"
        )


if __name__ == "__main__":
    unittest.main()
