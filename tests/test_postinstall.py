import plistlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import postinstall  # noqa: E402


class FakeGitHub:
    def __init__(self, text):
        self.text = text

    def get_file(self, repo, path, ref=None):
        return {"text": self.text}


class PostInstallTests(unittest.TestCase):
    def test_markdown_is_packaged_as_txt(self):
        url = "https://github.com/jarvissax/rondo/blob/main/docs/install.md#first-start"
        post = postinstall.resolve([url], FakeGitHub("# Installing Rondo\n\nSteps."))
        self.assertIsNotNone(post)
        self.assertEqual(post.markdown[0][0], url)
        name, data = post.render("windows")
        self.assertEqual(name, "3. After Install Steps.txt")
        text = data.decode("utf-8")
        self.assertIn("Source: " + url, text)
        self.assertIn("# Installing Rondo", text)
        self.assertEqual(post.render("macos")[0], "3. After Install Steps.txt")

    def test_html_link_makes_os_shortcut(self):
        post = postinstall.PostInstall(("https://innioasis.app/y2reborn_wifibt.html",))
        name, data = post.render("windows")
        self.assertEqual(name, "3. After Install Steps.url")
        self.assertIn(b"URL=https://innioasis.app/y2reborn_wifibt.html", data)
        name, data = post.render("linux")
        self.assertEqual(name, "3. After Install Steps.desktop")
        self.assertIn(b"Type=Link", data)
        name, data = post.render("macos")
        self.assertEqual(name, "3. After Install Steps.webloc")
        self.assertEqual(plistlib.loads(data)["URL"], "https://innioasis.app/y2reborn_wifibt.html")

    def test_no_links_produces_nothing(self):
        self.assertIsNone(postinstall.resolve([], FakeGitHub("")))
        self.assertIsNone(postinstall.PostInstall(()).render("windows"))


if __name__ == "__main__":
    unittest.main()
