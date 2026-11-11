import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402
from stats import (  # noqa: E402
    STATS_ASSET,
    build_stats,
    parse_stats_text,
    platform_for_name,
    prepare_baseline,
)


def asset(name, count):
    return {"name": name, "download_count": count}


def cfg():
    return mirrorspec.load_config(
        str(ROOT / "mirror.config.json"), mirror_repo="owner/mirror"
    )


SOURCE = {
    "tag_name": "3.1.2",
    "assets": [
        asset("rom.zip", 1000),
        asset("rom_y2.zip", 200),
        asset("updater.jpg", 10),
    ],
}

MIRROR = {
    "assets": [
        asset("rom.zip", 100),
        asset("rom-windows.zip", 250),
        asset("rom-linux.zip", 75),
        asset("rom-macos.zip", 125),
        asset("stats.txt", 999),
    ]
}

BLOCK = {
    "assets": [
        {"name": "rom-windows.zip", "platform": "windows"},
        {"name": "rom-linux.zip", "platform": "linux"},
        {"name": "rom-macos.zip", "platform": "macos"},
    ],
    "original_assets": ["rom.zip"],
    "stats": {
        "baseline": {"rom-windows.zip": 50},
        "retired": {"legacy.zip": 5},
    },
}


class StatsTests(unittest.TestCase):
    def test_platform_suffixes(self):
        self.assertEqual(platform_for_name("rom-windows-arm64.zip", None), "windows-arm64")
        self.assertEqual(platform_for_name("rom-linux-arm64.zip", None), "linux-arm64")
        self.assertEqual(
            platform_for_name("rom-windows-legacy.zip", None), "windows-legacy"
        )
        self.assertEqual(
            platform_for_name("rom-linux-legacy.zip", None), "linux-legacy"
        )
        self.assertEqual(platform_for_name("rom-windows.zip", None), "windows")
        self.assertIsNone(platform_for_name("rom.zip", None))
        self.assertEqual(platform_for_name("custom.zip", BLOCK), None)

    def test_cumulative_stats(self):
        report = build_stats(
            cfg(),
            SOURCE,
            MIRROR,
            BLOCK,
            replacing=False,
            generated_at="2026-11-03T00:00:00Z",
        )
        totals = report.totals
        self.assertEqual(totals["portable_windows_downloads"], 300)  # 50 baseline + 250
        self.assertEqual(totals["portable_linux_downloads"], 75)
        self.assertEqual(totals["portable_macos_downloads"], 125)
        self.assertEqual(totals["mirror_portable_downloads"], 500)
        self.assertEqual(totals["mirror_original_downloads"], 100)
        self.assertEqual(totals["mirror_retired_downloads"], 5)
        self.assertEqual(totals["mirror_total_downloads"], 605)
        self.assertEqual(totals["source_firmware_downloads"], 1200)
        self.assertEqual(totals["source_all_asset_downloads"], 1210)
        self.assertEqual(totals["combined_download_events"], 1805)
        self.assertIn("combined_download_events=1805", report.text)
        self.assertIn("[Totals]", report.text)
        self.assertNotIn("stats.txt", report.text)
        self.assertEqual(report.generated_at, "2026-11-03T00:00:00Z")

    def test_prepare_baseline_before_replacement(self):
        existing = [
            asset("rom-windows.zip", 250),
            asset("rom.zip", 100),
            asset("stats.txt", 999),
            asset("legacy.zip", 7),
        ]
        expected = {"rom-windows.zip", "rom.zip", STATS_ASSET}
        baseline, retired = prepare_baseline(BLOCK, existing, expected)
        self.assertEqual(baseline["rom-windows.zip"], 300)
        self.assertEqual(baseline["rom.zip"], 100)
        self.assertNotIn(STATS_ASSET, baseline)
        self.assertEqual(retired["legacy.zip"], 5)

    def test_prepare_baseline_retires_vanished_asset(self):
        block = {"stats": {"baseline": {"gone.zip": 9}, "retired": {}}}
        baseline, retired = prepare_baseline(block, [], set())
        self.assertEqual(baseline, {})
        self.assertEqual(retired["gone.zip"], 9)

    def test_replacing_uses_baseline_without_double_counting(self):
        report = build_stats(
            cfg(),
            SOURCE,
            MIRROR,
            BLOCK,
            baseline={"rom-windows.zip": 300, "rom.zip": 100},
            retired={"legacy.zip": 5},
            replacing=True,
            expected_names={
                "rom-windows.zip",
                "rom-linux.zip",
                "rom-macos.zip",
                "rom.zip",
                STATS_ASSET,
            },
            generated_at="2026-11-03T00:00:00Z",
        )
        self.assertEqual(report.totals["portable_windows_downloads"], 300)
        self.assertEqual(report.totals["portable_linux_downloads"], 75)
        self.assertEqual(report.totals["portable_macos_downloads"], 125)
        self.assertEqual(report.totals["mirror_original_downloads"], 100)

    def test_build_without_a_mirror(self):
        report = build_stats(cfg(), SOURCE, None, None)
        self.assertEqual(report.totals["mirror_total_downloads"], 0)
        self.assertEqual(report.totals["source_firmware_downloads"], 1200)
        self.assertIn("no mirror assets yet", report.text)

    def test_parse_stats_text_reads_totals_and_breakdown(self):
        report = build_stats(
            cfg(),
            SOURCE,
            MIRROR,
            BLOCK,
            replacing=False,
            generated_at="2026-11-03T00:00:00Z",
        )
        parsed = parse_stats_text(report.text)
        self.assertEqual(parsed["mirror"]["rom-windows.zip"], 300)
        self.assertEqual(parsed["totals"]["combined_download_events"], 1805)
        self.assertEqual(parsed["breakdown"]["total"], 1805)
        originals = {item["name"]: item for item in parsed["breakdown"]["originals"]}
        self.assertEqual(originals["rom.zip"]["combined"], 1600)
        self.assertEqual(originals["rom_y2.zip"]["combined"], 200)

    def test_previous_stats_keep_cumulative_counters_without_a_block(self):
        first = build_stats(
            cfg(),
            SOURCE,
            MIRROR,
            BLOCK,
            replacing=False,
            generated_at="2026-11-03T00:00:00Z",
        )
        previous = parse_stats_text(first.text)
        baseline, retired = prepare_baseline(
            None,
            [asset("rom-windows.zip", 250), asset("rom.zip", 100), asset("stats.txt", 9)],
            {"rom-windows.zip", "rom.zip", STATS_ASSET},
            previous_stats=previous,
        )
        self.assertEqual(baseline["rom-windows.zip"], 300)
        self.assertEqual(baseline["rom.zip"], 100)
        report = build_stats(
            cfg(),
            SOURCE,
            MIRROR,
            None,
            baseline=baseline,
            retired=retired,
            replacing=True,
            expected_names={"rom-windows.zip", "rom.zip", STATS_ASSET},
            previous_stats=previous,
            generated_at="2026-11-04T00:00:00Z",
        )
        self.assertEqual(report.totals["portable_windows_downloads"], 300)
        self.assertEqual(report.totals["mirror_retired_downloads"], 5)
        self.assertEqual(report.totals["combined_download_events"], 1805)


if __name__ == "__main__":
    unittest.main()
