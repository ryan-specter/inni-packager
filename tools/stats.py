"""Cumulative download statistics for mirrored firmware releases.

GitHub reports a lifetime ``download_count`` per release asset, but replacing
an asset with ``--clobber`` resets that counter.  This module keeps a ledger in
the release's hidden build block: when an asset is about to be replaced, its
current count is folded into a baseline, so the next ``stats.txt`` still shows
the full history.

The generated ``stats.txt`` separates:

* downloads of the original firmware assets on the source repository
* downloads of the original copies, portable packages and retired assets on
  the mirror repository
* a combined estimate covering every installation route, because all routes
  ultimately fetch one of those two sets of assets
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mirrorspec import MirrorConfig, firmware_asset_name_matches

STATS_ASSET = "stats.txt"

# Suffix -> platform key.  Longest suffixes are listed first so
# ``-windows-arm64.zip`` is not mistaken for ``-windows.zip``.
SUFFIX_PLATFORMS: Tuple[Tuple[str, str], ...] = (
    ("-windows-arm64.zip", "windows-arm64"),
    ("-linux-arm64.zip", "linux-arm64"),
    ("-windows.zip", "windows"),
    ("-linux.zip", "linux"),
    ("-macos.zip", "macos"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _downloads(assets: Optional[Sequence[Mapping[str, Any]]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for asset in assets or []:
        name = str(asset.get("name") or "")
        if not name or name == STATS_ASSET:
            continue
        counts[name] = int(asset.get("download_count") or 0)
    return counts


def platform_for_name(name: str, block: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return the platform key for an asset, using the build block first."""

    if block:
        for item in block.get("assets") or []:
            if item.get("name") == name:
                return str(item.get("platform") or "") or None
    lower = name.lower()
    for suffix, platform in SUFFIX_PLATFORMS:
        if lower.endswith(suffix):
            return platform
    return None


def prepare_baseline(
    block: Optional[Mapping[str, Any]],
    existing_assets: Sequence[Mapping[str, Any]],
    expected_names: Iterable[str],
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Snapshot counts before a rebuild replaces assets.

    Assets that will be re-uploaded have their current count added to the
    baseline (their new asset starts at zero).  Assets that stay in the release
    keep their live count and are not added.  Assets that disappeared are
    folded into ``retired``.
    """

    stats = (block or {}).get("stats") or {}
    baseline = {str(k): int(v) for k, v in (stats.get("baseline") or {}).items()}
    retired = {str(k): int(v) for k, v in (stats.get("retired") or {}).items()}
    counts = _downloads(existing_assets)
    expected = set(expected_names)

    for name, count in counts.items():
        if name in expected:
            baseline[name] = baseline.get(name, 0) + count

    for name, base in list(baseline.items()):
        if name not in counts and name not in expected:
            retired[name] = retired.get(name, 0) + base
            baseline.pop(name, None)

    return (
        dict(sorted(baseline.items())),
        dict(sorted(retired.items())),
    )


@dataclass
class StatsReport:
    text: str
    totals: Dict[str, int]
    baseline: Dict[str, int]
    retired: Dict[str, int]
    generated_at: str


def build_stats(
    cfg: MirrorConfig,
    source_release: Mapping[str, Any],
    mirror_release: Optional[Mapping[str, Any]],
    block: Optional[Mapping[str, Any]],
    *,
    baseline: Optional[Mapping[str, int]] = None,
    retired: Optional[Mapping[str, int]] = None,
    replacing: bool = False,
    expected_names: Optional[Iterable[str]] = None,
    generated_at: Optional[str] = None,
) -> StatsReport:
    """Build the stats document for one release.

    ``replacing`` means the assets in ``expected_names`` are about to be
    re-uploaded.  In that case the supplied ``baseline`` already includes their
    pre-upload counts, so their value is the baseline alone (adding the still
    visible count would double-count it).
    """

    block = block or {}
    stats_block = block.get("stats") or {}
    baseline_map = {
        str(k): int(v)
        for k, v in (baseline if baseline is not None else stats_block.get("baseline") or {}).items()
    }
    retired_map = {
        str(k): int(v)
        for k, v in (retired if retired is not None else stats_block.get("retired") or {}).items()
    }
    expected = set(expected_names or [])

    source_assets = list(source_release.get("assets") or [])
    source_counts = _downloads(source_assets)
    mirror_assets = list((mirror_release or {}).get("assets") or [])
    mirror_counts = _downloads(mirror_assets)

    mirror_values: Dict[str, int] = {}
    portable: Dict[str, int] = {platform.key: 0 for platform in cfg.platforms}
    original: Dict[str, int] = {}

    def add_value(name: str, value: int) -> None:
        mirror_values[name] = value
        platform = platform_for_name(name, block)
        if platform:
            portable[platform] = portable.get(platform, 0) + value
        else:
            original[name] = value

    for name, count in mirror_counts.items():
        if replacing and name in expected:
            # prepare_baseline() folds the pre-upload count into the baseline.
            # If a caller supplies an incomplete baseline, keep the live count
            # instead of silently dropping download history.
            value = baseline_map.get(name, count)
        else:
            value = baseline_map.get(name, 0) + count
        add_value(name, value)

    # A rebuild can reintroduce an asset whose baseline survived even though
    # the current release no longer has that file.
    for name, base in baseline_map.items():
        if name in mirror_counts:
            continue
        if replacing and name in expected:
            add_value(name, int(base))

    # Baseline entries whose asset vanished and is not being recreated are
    # historical downloads; keep them in the totals.
    orphaned = sum(
        int(base)
        for name, base in baseline_map.items()
        if name not in mirror_counts and name not in expected
    )
    retired_total = sum(int(v) for v in retired_map.values()) + orphaned

    source_firmware = sum(
        count for name, count in source_counts.items() if firmware_asset_name_matches(name, cfg)
    )
    source_all = sum(source_counts.values())
    portable_total = sum(portable.values())
    original_total = sum(original.values())
    mirror_total = portable_total + original_total + retired_total
    combined = source_firmware + mirror_total

    totals: Dict[str, int] = {
        "source_firmware_downloads": source_firmware,
        "source_all_asset_downloads": source_all,
        "mirror_portable_downloads": portable_total,
        "mirror_original_downloads": original_total,
        "mirror_retired_downloads": retired_total,
        "mirror_total_downloads": mirror_total,
        "combined_download_events": combined,
    }
    for key, value in sorted(portable.items()):
        totals[f"portable_{key}_downloads"] = value

    stamp = generated_at or utcnow()
    lines: List[str] = [
        "Portable Flashing Package statistics",
        "====================================",
        f"Repository : {cfg.mirror_repo or '(dry run)'}",
        f"Source     : {cfg.source_repo}",
        f"Release    : {source_release.get('tag_name') or ''}",
        f"Generated  : {stamp}",
        "",
        "Download counts are GitHub release-asset download events, not unique",
        "people. One person can count more than once, and GitHub may lag.",
        "",
        "Source downloads (original repository)",
    ]
    if source_counts:
        for name in sorted(source_counts):
            lines.append(f"  {name:<46}{source_counts[name]:>12,}")
    else:
        lines.append("  (no source assets)")

    lines += ["", "Mirror downloads (this repository, cumulative)"]
    if mirror_values:
        for name in sorted(mirror_values):
            lines.append(f"  {name:<46}{mirror_values[name]:>12,}")
    else:
        lines.append("  (no mirror assets yet)")
    if retired_total:
        lines.append(f"  {'retired/replaced assets':<46}{retired_total:>12,}")

    lines += ["", "Portable packages by platform"]
    for key in sorted(portable):
        lines.append(f"  {key:<46}{portable[key]:>12,}")
    lines.append(f"  {'portable total':<46}{portable_total:>12,}")

    lines += [
        "",
        "Coverage and estimate",
        "  The source firmware count includes manual SP Flash Tool downloads,",
        "  Innioasis Shared / Universal Flash Tool, Updater CE and Firmware",
        "  Archive links: they all fetch the same original release asset.",
        "  Mirror portable counts are the repackaged Windows/Linux/macOS",
        "  packages. Original copies and retired assets are listed separately",
        "  so replacing a file does not erase its history.",
        "  The combined figure counts download events across both sources; it",
        "  is an upper-bound activity estimate, not a count of unique installs.",
        "",
        "[Totals]",
    ]
    for key in (
        "source_firmware_downloads",
        "source_all_asset_downloads",
        "mirror_portable_downloads",
        "mirror_original_downloads",
        "mirror_retired_downloads",
        "mirror_total_downloads",
        "combined_download_events",
    ):
        lines.append(f"{key}={totals[key]}")
    for key in sorted(portable):
        lines.append(f"portable_{key}_downloads={portable[key]}")

    return StatsReport(
        text="\n".join(lines) + "\n",
        totals=totals,
        baseline=dict(sorted(baseline_map.items())),
        retired=dict(sorted(retired_map.items())),
        generated_at=stamp,
    )
