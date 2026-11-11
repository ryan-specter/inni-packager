import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import repos  # noqa: E402


class RepoFallbackTests(unittest.TestCase):
    def test_org_repo_gets_the_fallback_owner(self):
        self.assertEqual(
            repos.repo_variants("y1-community/Innioasis-Updater"),
            ["y1-community/Innioasis-Updater", "ryan-specter/Innioasis-Updater"],
        )

    def test_other_repositories_are_not_rewritten(self):
        self.assertEqual(
            repos.repo_variants("rockbox-y1/rockbox"), ["rockbox-y1/rockbox"]
        )

    def test_release_asset_urls_keep_primary_first(self):
        urls = repos.release_asset_urls(
            "y1-community/Innioasis-Updater", "v1", "rom.zip"
        )
        self.assertEqual(len(urls), 2)
        self.assertIn("y1-community", urls[0])
        self.assertIn("ryan-specter", urls[1])

    def test_raw_urls_get_the_fallback_owner(self):
        urls = repos.url_variants(
            "https://raw.githubusercontent.com/y1-community/Innioasis-Updater/main/config.ini"
        )
        self.assertEqual(len(urls), 2)
        self.assertIn("ryan-specter/Innioasis-Updater", urls[1])

    def test_manifest_tries_github_then_site(self):
        urls = repos.manifest_urls()
        self.assertTrue(urls[0].startswith("https://raw.githubusercontent.com/"))
        self.assertIn("ryan-specter", urls[1])
        self.assertEqual(urls[-1], "https://innioasis.app/slidia_manifest.xml")

    def test_tokens_accept_an_optional_prefix(self):
        token = "a" * 30
        self.assertEqual(repos.normalize_pat(token), "github_pat_" + token)
        self.assertEqual(
            repos.normalize_pat("github_pat_" + token), "github_pat_" + token
        )

    def test_ini_parser_accepts_key_and_token_rows(self):
        body = "key_0 = %s\ntoken = github_pat_%s\n" % ("b" * 30, "c" * 30)
        tokens = repos.tokens_from_ini(body)
        self.assertEqual(tokens[0], "github_pat_" + "b" * 30)
        self.assertEqual(tokens[1], "github_pat_" + "c" * 30)


if __name__ == "__main__":
    unittest.main()
