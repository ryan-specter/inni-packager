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

import json
import re
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
    ("-windows-legacy.zip", "windows-legacy"),
    ("-linux-legacy.zip", "linux-legacy"),
    ("-windows.zip", "windows"),
    ("-linux.zip", "linux"),
    ("-macos.zip", "macos"),
)

PLATFORM_LABELS: Dict[str, str] = {
    "windows": "Portable zip for Windows (x86-64)",
    "windows-arm64": "Portable zip for Windows on ARM",
    "windows-legacy": "Portable zip for Windows XP/7/8 (32/64-bit)",
    "linux": "Portable zip for Linux (x86-64)",
    "linux-arm64": "Portable zip for Linux arm64",
    "linux-legacy": "Portable zip for 32-bit Linux",
    "macos": "Portable zip for macOS (Intel + Apple silicon)",
}


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


def _count_line(line: str) -> Optional[Tuple[str, int]]:
    """Parse one ``name    count`` line from a stats.txt section."""

    match = re.match(r"^\s{2}(.+?)\s+([\d,]+)\s*$", line)
    if not match:
        return None
    return match.group(1).strip(), int(match.group(2).replace(",", ""))


def parse_stats_text(text: str) -> Dict[str, Any]:
    """Read a published stats.txt back into cumulative values.

    The file is deliberately human-readable; this parser only relies on the
    stable section headings and the ``[Totals]``/``[Breakdown]`` blocks.  It is
    used before a rebuild replaces assets, so an older release without a build
    ledger can still keep its full download history.
    """

    result: Dict[str, Any] = {
        "source": {},
        "mirror": {},
        "portable": {},
        "totals": {},
        "generated_at": "",
        "breakdown": None,
    }
    lines = str(text or "").splitlines()
    section = ""
    for index, raw in enumerate(lines):
        line = raw.rstrip()
        if line.startswith("Generated  :"):
            result["generated_at"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Source downloads"):
            section = "source"
            continue
        if line.startswith("Mirror downloads"):
            section = "mirror"
            continue
        if line.startswith("Portable packages by platform"):
            section = "portable"
            continue
        if line.strip() == "[Totals]":
            section = "totals"
            continue
        if line.strip() == "[Breakdown]":
            for later in lines[index + 1 :]:
                if later.strip():
                    try:
                        parsed = json.loads(later)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict):
                        result["breakdown"] = parsed
                    break
            section = "breakdown"
            continue
        if section == "totals" and "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                try:
                    result["totals"][key] = int(value.strip().replace(",", ""))
                except ValueError:
                    pass
            continue
        if section in ("source", "mirror", "portable"):
            parsed_line = _count_line(line)
            if parsed_line:
                name, count = parsed_line
                result[section][name] = count
    return result


def _history_totals(previous_stats: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    totals = (previous_stats or {}).get("totals") or {}
    return {
        key: int(totals.get(key) or 0)
        for key in ("mirror_retired_downloads", "combined_download_events")
    }


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
    previous_stats: Optional[Mapping[str, Any]] = None,
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

    cumulative = {
        str(name): int(value or 0)
        for name, value in ((previous_stats or {}).get("mirror") or {}).items()
        if str(name) != STATS_ASSET
    }
    if cumulative:
        # stats.txt already contains the cumulative figure. For an asset that
        # will be re-uploaded, that figure becomes the new baseline; for an
        # asset that stays in place, only the part above its live counter is a
        # baseline. This recovers history for releases built before the
        # ledger existed.
        for name, total in cumulative.items():
            live = counts.get(name, 0)
            if name in expected:
                baseline[name] = max(baseline.get(name, 0), total)
            elif name in counts:
                baseline[name] = max(baseline.get(name, 0), max(0, total - live))
            else:
                retired[name] = max(retired.get(name, 0), total)
        for name in expected:
            if name in counts and name not in cumulative:
                baseline[name] = baseline.get(name, 0) + counts[name]
    else:
        for name, count in counts.items():
            if name in expected:
                baseline[name] = baseline.get(name, 0) + count

    for name, base in list(baseline.items()):
        if name not in counts and name not in expected:
            retired[name] = retired.get(name, 0) + base
            baseline.pop(name, None)

    retired_total = _history_totals(previous_stats).get("mirror_retired_downloads", 0)
    if retired_total > sum(retired.values()):
        retired["__historical_retired__"] = retired_total - sum(retired.values())

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
    breakdown: Dict[str, Any]


def build_breakdown(
    cfg: MirrorConfig,
    source_release: Mapping[str, Any],
    block: Optional[Mapping[str, Any]],
    source_counts: Mapping[str, int],
    mirror_values: Mapping[str, int],
    retired_total: int,
    combined_total: int,
) -> Dict[str, Any]:
    """Group mirror and source downloads by the original firmware package."""

    block = block or {}
    portable: Dict[str, Dict[str, str]] = {}
    for item in block.get("assets") or []:
        name = str(item.get("name") or "")
        platform = str(item.get("platform") or "")
        if not name or not platform:
            continue
        portable[name] = {
            "platform": platform,
            "source_asset": str(item.get("source_asset") or name),
        }

    groups: Dict[str, Dict[str, Any]] = {}

    def group(name: str) -> Dict[str, Any]:
        return groups.setdefault(
            name,
            {
                "name": name,
                "source_downloads": 0,
                "mirror_original_downloads": 0,
                "portable": [],
                "combined": 0,
            },
        )

    for name, count in source_counts.items():
        if firmware_asset_name_matches(name, cfg):
            group(name)["source_downloads"] = int(count)
    for name in block.get("original_assets") or []:
        if firmware_asset_name_matches(str(name), cfg):
            group(str(name))
    for name, count in mirror_values.items():
        if name in portable:
            group(portable[name]["source_asset"])
        elif firmware_asset_name_matches(name, cfg):
            group(name)["mirror_original_downloads"] = int(count)

    for name, meta in portable.items():
        target = group(meta["source_asset"])
        target["portable"].append(
            {
                "platform": meta["platform"],
                "name": name,
                "label": PLATFORM_LABELS.get(meta["platform"], "Portable zip"),
                "downloads": int(mirror_values.get(name, 0)),
            }
        )

    ordered: List[Dict[str, Any]] = []
    for name in sorted(groups):
        entry = groups[name]
        entry["portable"].sort(key=lambda item: item["platform"])
        entry["combined"] = (
            int(entry["source_downloads"])
            + int(entry["mirror_original_downloads"])
            + sum(int(item["downloads"]) for item in entry["portable"])
        )
        ordered.append(entry)

    grouped_total = sum(int(entry["combined"]) for entry in ordered)
    return {
        "total": int(combined_total),
        "originals": ordered,
        "retired_downloads": int(retired_total),
        "other_downloads": max(0, int(combined_total) - grouped_total - int(retired_total)),
    }


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
    previous_stats: Optional[Mapping[str, Any]] = None,
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
            previous = ((previous_stats or {}).get("mirror") or {}).get(name)
            if previous is not None:
                value = max(value, int(previous or 0))
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
    retired_history = _history_totals(previous_stats).get("mirror_retired_downloads", 0)
    if retired_history > retired_total:
        retired_total = retired_history

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

    breakdown = build_breakdown(
        cfg,
        source_release,
        block,
        source_counts,
        mirror_values,
        retired_total,
        combined,
    )

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

    lines += ["", "Downloads by original package"]
    if breakdown["originals"]:
        for entry in breakdown["originals"]:
            lines.append(f"  {entry['name']}")
            if entry["source_downloads"]:
                lines.append(f"    {'source downloads':<42}{entry['source_downloads']:>12,}")
            if entry["mirror_original_downloads"]:
                lines.append(f"    {'mirror original':<42}{entry['mirror_original_downloads']:>12,}")
            for package in entry["portable"]:
                lines.append(
                    f"    {package['label']:<42}{package['downloads']:>12,}"
                )
            lines.append(f"    {'combined':<42}{entry['combined']:>12,}")
    else:
        lines.append("  (no firmware ZIPs)")
    if breakdown["retired_downloads"]:
        lines.append(f"  {'retired/replaced assets':<46}{breakdown['retired_downloads']:>12,}")
    if breakdown["other_downloads"]:
        lines.append(f"  {'other mirrored files':<46}{breakdown['other_downloads']:>12,}")

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

    lines += [
        "",
        "[Breakdown]",
        json.dumps(breakdown, sort_keys=True, separators=(",", ":")),
    ]

    return StatsReport(
        text="\n".join(lines) + "\n",
        totals=totals,
        baseline=dict(sorted(baseline_map.items())),
        retired=dict(sorted(retired_map.items())),
        generated_at=stamp,
        breakdown=breakdown,
    )
