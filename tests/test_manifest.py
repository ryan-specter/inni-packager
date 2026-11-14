import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import manifest  # noqa: E402
import mirrorspec  # noqa: E402

SAMPLE = """<?xml version="1.0"?>
<slidia>
  <!-- <package name="APK" repo="x/y" type="apk" /> -->
  <package name="Original Software" repo="y1-community/y1-stock-rom" device="Y1" type="img" />
  <package name="Original Software" repo="y1-community/y1-stock-rom" device="Y2" type="img" />
  <package name="Rockbox" repo="rockbox-y1/rockbox" device="Y1" type="img"
           post_install="https://github.com/rockbox-y1/rockbox/blob/main/INSTALL.md" />
  <package name="Y2 Player" repo="schulzcode/Y2Player" device="Y2" type="img" />
  <package name="Ignored" repo="not-a-repo" device="Y1" type="img" />
  <package name="Ignored apk" repo="a/b" device="Y1" type="apk" />
</slidia>
"""


class FetchManifestTests(unittest.TestCase):
    def test_http_fetch_adds_a_cache_busting_query(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<slidia/>"
        with mock.patch.object(
            manifest.urllib.request, "urlopen", return_value=response
        ) as opened:
            self.assertEqual(
                manifest.fetch_manifest("https://example.com/slidia_manifest.xml"),
                "<slidia/>",
            )
        url = opened.call_args.args[0].full_url
        self.assertTrue(url.startswith("https://example.com/slidia_manifest.xml?"))
        self.assertIn("_pfp=", url)


    def test_raw_github_manifest_uses_the_contents_api(self):
        self.assertEqual(
            manifest._github_api_url_for_raw(
                "https://raw.githubusercontent.com/y1-community/Innioasis-Updater/main/slidia_manifest.xml"
            ),
            "https://api.github.com/repos/y1-community/Innioasis-Updater/contents/slidia_manifest.xml?ref=main",
        )
        self.assertIsNone(
            manifest._github_api_url_for_raw(
                "https://innioasis.app/slidia_manifest.xml"
            )
        )


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.packages = manifest.parse_manifest(SAMPLE)
        self.products = manifest.group_products(self.packages)

    def test_comments_and_apks_are_ignored(self):
        self.assertEqual(len(self.packages), 4)
        self.assertEqual(
            [p.repo for p in self.packages],
            [
                "y1-community/y1-stock-rom",
                "y1-community/y1-stock-rom",
                "rockbox-y1/rockbox",
                "schulzcode/Y2Player",
            ],
        )

    def test_one_product_per_source_repository(self):
        self.assertEqual(len(self.products), 3)
        stock = self.products[0]
        self.assertEqual(stock.source_repo, "y1-community/y1-stock-rom")
        self.assertEqual(stock.devices, ["Y1", "Y2"])
        self.assertEqual(stock.software_name, "Original Software")
        self.assertEqual(stock.mirror_repo, "y1-community/y1-stock-rom-mirror")

    def test_names_are_lowercase_and_suffix_is_not_doubled(self):
        self.assertEqual(
            self.products[1].mirror_repo, "y1-community/rockbox-mirror"
        )
        self.assertEqual(
            self.products[2].mirror_repo, "y1-community/y2player-mirror"
        )
        again = manifest.group_products(
            self.packages, suffix="-mirror"
        )
        self.assertEqual(again[1].mirror_repo, "y1-community/rockbox-mirror")

    def test_post_install_is_preserved(self):
        self.assertEqual(
            self.products[1].post_installs,
            ["https://github.com/rockbox-y1/rockbox/blob/main/INSTALL.md"],
        )

    def test_generated_config_has_model_and_type_rules(self):
        cfg = manifest.product_config(self.products[0])
        self.assertEqual(cfg["source_repo"], "y1-community/y1-stock-rom")
        self.assertEqual(cfg["mirror_repo"], "y1-community/y1-stock-rom-mirror")
        self.assertEqual(cfg["defaults"]["target_model"], "Y1")
        self.assertEqual(cfg["defaults"]["target_type"], "Type A")
        self.assertTrue(cfg["source_readme"]["enabled"])
        self.assertEqual(cfg["source_readme"]["path"], "README.md")
        patterns = [rule["match_asset"] for rule in cfg["rules"]]
        self.assertTrue(any("type[-_ ]?b" in pattern for pattern in patterns))
        self.assertTrue(any("y2" in pattern for pattern in patterns))

    def test_y2_only_product_defaults_to_stock(self):
        cfg = manifest.product_config(self.products[2])
        self.assertEqual(cfg["defaults"]["target_model"], "Y2")
        self.assertEqual(cfg["defaults"]["target_type"], "Stock")

    def test_generated_config_loads_in_the_packager(self):
        cfg = manifest.product_config(self.products[0])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mirror.config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            loaded = mirrorspec.load_config(str(path))
        self.assertEqual(loaded.source_repo, "y1-community/y1-stock-rom")
        self.assertEqual(loaded.mirror_repo, "y1-community/y1-stock-rom-mirror")
        self.assertEqual(
            [p.key for p in loaded.platforms],
            [
                "windows",
                "windows-arm64",
                "windows-legacy",
                "linux",
                "linux-arm64",
                "linux-legacy",
                "macos",
            ],
        )

    def test_caller_workflow_uses_the_central_repository(self):
        workflow = manifest.caller_workflow(
            "ryan-specter/inni-packager", "main"
        )
        self.assertIn(
            "uses: ryan-specter/inni-packager/.github/workflows/reusable-mirror.yml@main",
            workflow,
        )
        self.assertIn("secrets: inherit", workflow)
        self.assertIn("managed-by: ryan-specter/inni-packager", workflow)


UNIFIED = """<?xml version="1.0"?>
<slidia>
  <package name="inniclassic" repo="JonaszPotoniec/inniclassic-y2" device="Y1" type="img" />
  <package name="inniclassic" repo="JonaszPotoniec/inniclassic-y2" device="Y2" type="img" />
</slidia>
"""


class UnifiedProductTests(unittest.TestCase):
    """A repo that ships Y1 Type A/B and Y2 firmware is one product.

    The Y1 aliases ``rom.zip`` and ``rom_type_b.zip`` are the same bytes as
    the explicit ``rom_y1_type_*`` assets, so they must never inherit the Y2
    defaults of a Y2-only product.
    """

    def setUp(self):
        packages = manifest.parse_manifest(UNIFIED)
        self.products = manifest.group_products(packages)
        self.product = self.products[0]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "mirror.config.json"
        path.write_text(
            json.dumps(manifest.product_config(self.product)), encoding="utf-8"
        )
        self.cfg = mirrorspec.load_config(str(path))

    def test_single_mirror_for_both_devices(self):
        self.assertEqual(len(self.products), 1)
        self.assertEqual(
            self.product.mirror_repo, "y1-community/inniclassic-y2-mirror"
        )
        self.assertEqual(self.product.devices, ["Y1", "Y2"])

    def test_legacy_aliases_stay_y1(self):
        expected = {
            "rom.zip": ("Y1", "Type A"),
            "rom_type_b.zip": ("Y1", "Type B"),
            "rom_y1_type_a.zip": ("Y1", "Type A"),
            "rom_y1_type_b.zip": ("Y1", "Type B"),
            "rom_y2.zip": ("Y2", "Stock"),
        }
        for asset, (model, kind) in expected.items():
            with self.subTest(asset=asset):
                meta = mirrorspec.resolve_metadata(self.cfg, asset)
                self.assertEqual(meta["target_model"], model)
                self.assertEqual(meta["target_type"], kind)


COMMENTED = """<?xml version="1.0"?>
<slidia>
  <!-- <package name="APK" repo="a/b" type="apk" /> -->
  <package name="Original Software" repo="y1-community/y1-stock-rom" device="Y1" type="img" />
  <!-- <package name="Original Software" brand="Innioasis" repo="y1-community/g1-stock-rom" device="G1" type="img" package_name="rom_g1.zip" /> -->
  <!-- <package name="Original Software" brand="Timmkoo" repo="y1-community/q5-stock-rom" device="Q5" type="img" package_name="rom_q5.zip" /> -->
  <!-- <package name="Original Software" brand="Timmkoo" repo="y1-community/q5-stock-rom" device="Q3e" type="img" package_name="rom_q5.zip" /> -->
  <!-- <package name="Original Software" repo="y1-community/y1-stock-rom" device="Y1" type="img" /> -->
</slidia>
"""


class CommentedProductTests(unittest.TestCase):
    """Stock ROMs are commented out until the app lists the model."""

    def setUp(self):
        self.active = manifest.parse_manifest(COMMENTED)
        self.all = manifest.parse_manifest(COMMENTED, include_commented=True)
        self.products = manifest.group_products(self.all)

    def test_commented_rows_are_opt_in(self):
        self.assertEqual([p.repo for p in self.active], ["y1-community/y1-stock-rom"])
        self.assertEqual(len(self.all), 4)
        self.assertNotIn("a/b", [p.repo for p in self.all])

    def test_each_model_gets_its_own_mirror(self):
        mirrors = {product.source_repo: product.mirror_repo for product in self.products}
        self.assertEqual(
            mirrors["y1-community/g1-stock-rom"],
            "y1-community/g1-stock-rom-mirror",
        )
        self.assertEqual(
            mirrors["y1-community/q5-stock-rom"],
            "y1-community/q5-stock-rom-mirror",
        )

    def test_a_model_name_is_not_coerced_to_y1(self):
        g1 = next(p for p in self.products if "g1-stock-rom" in p.source_repo)
        cfg = manifest.product_config(g1)
        self.assertEqual(cfg["defaults"]["target_model"], "G1")
        self.assertEqual(cfg["defaults"]["target_type"], "Stock")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mirror.config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            loaded = mirrorspec.load_config(str(path))
        meta = mirrorspec.resolve_metadata(loaded, "rom_g1.zip")
        self.assertEqual(meta["target_model"], "G1")
        self.assertEqual(meta["target_type"], "Stock")

    def test_one_repository_serving_two_models_is_one_product(self):
        q5 = next(p for p in self.products if "q5-stock-rom" in p.source_repo)
        self.assertEqual(q5.devices, ["Q5", "Q3E"])
        self.assertEqual(manifest.product_config(q5)["defaults"]["target_model"], "Q5")


DUPLICATE_ATTRIBUTE = """<?xml version="1.0"?>
<slidia>
  <package name="Lithium" brand="Timmkoo" repo="NoahDomingues/Lithium" device="Q3" type="img"
           install_first="y1-community/q3-stock-rom" install_first="y1-community/q3-stock-rom" />
  <package name="Rondo" repo="jarvissax/rondo" device="Y1" type="img" />
</slidia>
"""

BROKEN_XML = """<?xml version="1.0"?>
<slidia>
  <package name="Rondo & Rondo" repo="jarvissax/rondo" device="Y1" type="img" />
  <package name="Rockbox" repo="rockbox-y1/rockbox" device="Y1" type="img" />
</slidia>
"""


class HandEditedManifestTests(unittest.TestCase):
    """A defect in the hand-maintained catalogue must not stop packaging.

    Every mirror is dispatched from this manifest.  When it fails to parse,
    onboarding fails and no portable package is rebuilt at all, so a typo in
    another repository would silently leave every download stale.
    """

    def test_a_repeated_attribute_is_repaired_not_fatal(self):
        with mock.patch("sys.stderr"):
            packages = manifest.parse_manifest(DUPLICATE_ATTRIBUTE)
        self.assertEqual([p.repo for p in packages], ["NoahDomingues/Lithium", "jarvissax/rondo"])
        self.assertEqual(
            [p.device for p in packages],
            ["Q3", "Y1"],
        )

    def test_the_repair_keeps_the_first_value(self):
        repaired, removed = manifest._repair_duplicate_attributes(
            '<package repo="a/b" device="Y1" device="Y2" type="img" />'
        )
        self.assertEqual(removed, 1)
        self.assertIn('device="Y1"', repaired)
        self.assertNotIn('device="Y2"', repaired)

    def test_a_tag_value_may_contain_a_greater_than_sign(self):
        repaired, removed = manifest._repair_duplicate_attributes(
            '<package repo="a/b" install_first_version="3.2.0=>" '
            'install_first_version="3.2.0=>" type="img" />'
        )
        self.assertEqual(removed, 1)
        self.assertIn('install_first_version="3.2.0=>"', repaired)

    def test_commented_rows_are_not_repaired(self):
        repaired, removed = manifest._repair_duplicate_attributes(
            '<!-- <package repo="a/b" device="Y1" device="Y1" /> -->'
        )
        self.assertEqual(removed, 0)
        self.assertEqual(repaired.count('device="Y1"'), 2)

    def test_any_other_defect_falls_back_to_the_rows(self):
        with mock.patch("sys.stderr"):
            packages = manifest.parse_manifest(BROKEN_XML)
        self.assertEqual(
            [p.repo for p in packages],
            ["jarvissax/rondo", "rockbox-y1/rockbox"],
        )

    def test_a_manifest_with_no_readable_rows_still_fails(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(ValueError):
                manifest.parse_manifest("<html><body>not found</body>")


if __name__ == "__main__":
    unittest.main()
