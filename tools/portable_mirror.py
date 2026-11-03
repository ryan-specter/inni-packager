#!/usr/bin/env python3
"""Mirror another repository's firmware releases as portable flashing packages.

Each source release is republished under the same tag, with the same title
(plus ``Portable Flashing Package: ``) and notes, all original assets, and one
extra zip per platform:

    rom.zip  ->  rom-windows.zip, rom-linux.zip, rom-linux-arm64.zip, rom-macos.zip

Usage:
    python tools/portable_mirror.py sync
    python tools/portable_mirror.py status
    python tools/portable_mirror.py build --tag 3.1.2 --out dist/3.1.2
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

from gh import ApiError, GitHub, GitHubError
from mirrorspec import (
    BUILDER_VERSION,
    ConfigError,
    MirrorConfig,
    Platform,
    fw_package_ini,
    load_config,
    parse_block,
    release_fingerprint,
    release_title,
    release_version,
    render_notes,
    resolve_metadata,
    output_name,
    select_firmware_assets,
)
from packager import (
    BuildResult,
    PackagingError,
    build_portable_package,
    inspect_firmware,
)
from stats import STATS_ASSET, StatsReport, build_stats, prepare_baseline

INDEX_SCHEMA = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Log:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.lines: List[str] = []
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.lines.append(message)
            print(f"[{stamp}] {message}", flush=True)

    def debug(self, message: str) -> None:
        if self.verbose:
            self(message)


def parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def release_tag(release: Mapping[str, Any]) -> str:
    return str(release.get("tag_name") or "")


def find_updater_asset(
    releases: Sequence[Mapping[str, Any]], platform: Platform
) -> Optional[Tuple[Mapping[str, Any], Mapping[str, Any]]]:
    wanted = platform.updater_asset.lower()
    for release in releases:
        if release.get("draft"):
            continue
        tag = release_tag(release)
        if platform.pinned_tag and tag != platform.pinned_tag:
            continue
        for asset in release.get("assets") or []:
            if str(asset.get("name") or "").lower() == wanted:
                return release, asset
    return None


def resolve_updater_assets(
    cfg: MirrorConfig, gh: GitHub, log: Log
) -> Dict[str, Dict[str, Any]]:
    releases = gh.list_releases(cfg.updater_repo)
    chosen: Dict[str, Dict[str, Any]] = {}
    for platform in cfg.platforms:
        found = find_updater_asset(releases, platform)
        if not found:
            if platform.pinned_tag:
                raise SystemExit(
                    f"updater {cfg.updater_repo} has no {platform.updater_asset} "
                    f"in pinned release {platform.pinned_tag}"
                )
            if platform.optional:
                log(f"  skipping {platform.key}: no {platform.updater_asset} published yet")
                continue
            raise SystemExit(
                f"updater {cfg.updater_repo} has no asset named {platform.updater_asset}; "
                f"cannot build {platform.key}"
            )
        release, asset = found
        chosen[platform.key] = {
            "tag": release_tag(release),
            "name": str(asset["name"]),
            "size": int(asset.get("size") or 0),
            "id": asset.get("id"),
            "url": asset.get("browser_download_url"),
        }
        log(f"  updater {platform.key}: {asset['name']} from {release_tag(release)}")
    if not chosen:
        raise SystemExit("no updater assets are available for any enabled platform")
    return chosen


def filter_source_releases(
    cfg: MirrorConfig, releases: Sequence[Mapping[str, Any]], args
) -> List[Dict[str, Any]]:
    allowed = cfg.tags_allow
    if args.tags:
        allowed = parse_csv(args.tags)
    excluded = cfg.tags_exclude
    selected: List[Dict[str, Any]] = []
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not cfg.include_prereleases:
            continue
        tag = release_tag(release)
        if allowed and not any(fnmatch.fnmatch(tag, pattern) for pattern in allowed):
            continue
        if any(fnmatch.fnmatch(tag, pattern) for pattern in excluded):
            continue
        selected.append(dict(release))
    if getattr(args, "offset", 0):
        selected = selected[args.offset :]
    if args.limit:
        selected = selected[: args.limit]
    return selected


def block_is_current(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    existing: Optional[Mapping[str, Any]],
    block: Optional[Mapping[str, Any]],
    fingerprint: str,
) -> bool:
    if not existing or not block:
        return False
    if block.get("v") != BUILDER_VERSION or block.get("fingerprint") != fingerprint:
        return False
    if block.get("failures"):
        # A partial build must be retried even though the assets that did
        # upload are present at the right size.
        return False
    assets = block.get("assets")
    if not isinstance(assets, list) or not assets:
        return False
    current = {
        str(a.get("name")): int(a.get("size") or 0)
        for a in existing.get("assets") or []
        if a.get("state", "uploaded") == "uploaded"
    }
    for item in assets:
        name = str(item.get("name") or "")
        size = int(item.get("size") or 0)
        if not name or current.get(name) != size:
            return False
    return True


def build_index_entry(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    block: Mapping[str, Any],
) -> Dict[str, Any]:
    tag = release_tag(release)
    tag_q = quote(tag, safe="")
    assets = []
    for item in block.get("assets") or []:
        name = str(item.get("name") or "")
        assets.append(
            {
                "name": name,
                "source_asset": item.get("source_asset"),
                "platform": item.get("platform"),
                "size": int(item.get("size") or 0),
                "sha256": item.get("sha256"),
                "url": f"https://github.com/{cfg.mirror_repo}/releases/download/"
                f"{tag_q}/{quote(name, safe='')}",
            }
        )
    return {
        "tag": tag,
        "title": release_title(cfg, release),
        "source_title": str(release.get("name") or "").strip() or tag,
        "source_url": f"https://github.com/{cfg.source_repo}/releases/tag/{tag_q}",
        "published_at": release.get("published_at"),
        "prerelease": bool(release.get("prerelease")),
        "source_notes": str(release.get("body") or ""),
        "assets": assets,
        "original_assets": list(block.get("original_assets") or []),
        "stats_url": f"https://github.com/{cfg.mirror_repo}/releases/download/"
        f"{tag_q}/{STATS_ASSET}",
    }


def make_index(
    cfg: MirrorConfig,
    updates: Mapping[str, Optional[Mapping[str, Any]]],
    previous: Optional[Mapping[str, Any]],
    source_readme: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    entries: Dict[str, Dict[str, Any]] = {}
    if previous and isinstance(previous.get("releases"), list):
        for entry in previous["releases"]:
            if isinstance(entry, dict) and entry.get("tag"):
                entries[str(entry["tag"])] = entry
    for tag, entry in updates.items():
        if entry is None:
            entries.pop(tag, None)
        else:
            entries[tag] = dict(entry)
    ordered = sorted(
        entries.values(),
        key=lambda e: str(e.get("published_at") or ""),
        reverse=True,
    )
    index = {
        "schema": INDEX_SCHEMA,
        "generated_at": utcnow(),
        "source_repo": cfg.source_repo,
        "mirror_repo": cfg.mirror_repo,
        "title_prefix": cfg.title_prefix,
        "updater_repo": cfg.updater_repo,
        "platforms": [
            {
                "key": p.key,
                "os": p.os,
                "arch": p.arch,
                "suffix": p.suffix,
                "layout": p.layout,
            }
            for p in cfg.platforms
        ],
        "releases": ordered,
    }
    if source_readme:
        index["source_readme"] = {
            key: source_readme.get(key)
            for key in ("path", "source_path", "source_name", "source_url", "url")
        }
    return index


def write_index(cfg: MirrorConfig, index: Mapping[str, Any], path: Path, log: Log) -> bool:
    previous: Optional[Dict[str, Any]] = None
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None
    if previous:
        old = dict(previous)
        new = dict(index)
        old.pop("generated_at", None)
        new.pop("generated_at", None)
        if old == new:
            log(f"index unchanged: {path}")
            return False
    path.write_text(json.dumps(index, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    log(f"wrote index: {path}")
    return True


def download_updater_files(
    cfg: MirrorConfig,
    gh: GitHub,
    updater: Mapping[str, Mapping[str, Any]],
    work: Path,
    log: Log,
) -> Dict[str, Path]:
    paths: Dict[str, Path] = {}
    for key, info in updater.items():
        dest = work / "updater" / str(info["tag"]) / str(info["name"])
        asset = {
            "name": info["name"],
            "size": info["size"],
            "id": info.get("id"),
            "browser_download_url": info.get("url"),
        }
        log(f"  preparing updater for {key}")
        paths[key] = gh.download(cfg.updater_repo, asset, dest, log=log)
    return paths


def download_release_asset(
    cfg: MirrorConfig, gh: GitHub, asset: Mapping[str, Any], directory: Path, log: Log
) -> Path:
    name = str(asset["name"])
    dest = directory / name
    if dest.exists() and dest.stat().st_size == int(asset.get("size") or 0):
        return dest
    return gh.download(cfg.source_repo, asset, dest, log=log)


def build_for_release(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    existing: Optional[Mapping[str, Any]],
    existing_block: Optional[Mapping[str, Any]],
    gh: GitHub,
    updater: Mapping[str, Mapping[str, Any]],
    ensure_updater: Callable[[str], Path],
    work: Path,
    log: Log,
    dry_run: bool = False,
) -> Optional[Dict[str, Any]]:
    """Download, repackage and upload one release.  Returns a report entry."""

    tag = release_tag(release)
    source_assets = list(release.get("assets") or [])
    candidates = select_firmware_assets(source_assets, cfg)
    log(f"  firmware assets: {', '.join(str(a['name']) for a in candidates) or '(none)'}")
    if not candidates:
        return {"tag": tag, "action": "skipped", "reason": "no firmware zip assets"}

    src_dir = work / tag / "src"
    downloadable = []
    for asset in candidates:
        size = int(asset.get("size") or 0)
        if size > cfg.max_asset_bytes:
            log(f"  skipping {asset['name']}: larger than max_asset_bytes")
            continue
        downloadable.append(asset)
    if not downloadable:
        return {"tag": tag, "action": "skipped", "reason": "firmware assets exceed max_asset_bytes"}

    firmware: List[Tuple[Mapping[str, Any], Path, Any]] = []
    skipped_assets: List[Dict[str, str]] = []
    for asset in downloadable:
        path = download_release_asset(cfg, gh, asset, src_dir, log)
        try:
            package = inspect_firmware(path)
        except PackagingError as exc:
            log(f"  skipping {asset['name']}: {exc}")
            skipped_assets.append({"name": str(asset["name"]), "reason": str(exc)})
            continue
        if cfg.require_scatter and not package.scatters:
            log(f"  skipping {asset['name']}: no *scatter*.txt (not an SP Flash Tool package)")
            skipped_assets.append(
                {"name": str(asset["name"]), "reason": "no scatter file"}
            )
            continue
        log(f"  {package.describe()}")
        firmware.append((asset, path, package))
    if not firmware:
        return {
            "tag": tag,
            "action": "skipped",
            "reason": "no flashable firmware zips",
            "skipped_assets": skipped_assets,
        }

    original_files: List[Tuple[str, Path]] = []
    if cfg.copy_original_assets:
        original_dir = work / tag / "original"
        downloaded = {str(asset["name"]): path for asset, path, _ in firmware}
        for asset in source_assets:
            name = str(asset.get("name") or "")
            if not name or name == STATS_ASSET:
                continue
            size = int(asset.get("size") or 0)
            if size > cfg.max_asset_bytes:
                log(f"  not copying original {name}: larger than max_asset_bytes")
                continue
            if name in downloaded:
                original_files.append((name, downloaded[name]))
                continue
            try:
                path = download_release_asset(cfg, gh, asset, original_dir, log)
                original_files.append((name, path))
            except (GitHubError, ApiError) as exc:
                log(f"  warning: could not copy original asset {name}: {exc}")

    out_dir = work / tag / "out"
    tasks = []
    for asset, _path, package in firmware:
        meta = resolve_metadata(cfg, str(asset["name"]))
        ini_text = fw_package_ini(
            software_name=meta["software_name"],
            version=release_version(tag),
            target_model=meta["target_model"],
            target_type=meta["target_type"],
            description=meta["description"] or str(release.get("name") or ""),
        )
        for platform in cfg.platforms:
            if platform.key not in updater:
                continue
            tasks.append(
                (
                    asset,
                    package,
                    platform,
                    out_dir / output_name(str(asset["name"]), platform),
                    ini_text,
                    meta,
                )
            )
    if not tasks:
        return {"tag": tag, "action": "skipped", "reason": "no platforms available"}

    results: Dict[str, BuildResult] = {}
    failures: List[str] = []

    def run(task) -> BuildResult:
        asset, package, platform, out_path, ini_text, meta = task
        return build_portable_package(
            ensure_updater(platform.key),
            package,
            platform,
            out_path,
            ini_text,
            meta["software_name"],
            release_version(tag),
            str(asset["name"]),
            log=log,
        )

    with ThreadPoolExecutor(max_workers=cfg.build_workers) as pool:
        futures = {pool.submit(run, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # keep other platforms going
                failures.append(f"{task[0]['name']} / {task[2].key}: {exc}")
                log(f"  ERROR building {task[0]['name']} / {task[2].key}: {exc}")
                continue
            key = f"{result.source_asset}::{result.platform}"
            results[key] = result
    if not results:
        return {
            "tag": tag,
            "action": "failed",
            "reason": "; ".join(failures) or "packaging failed",
        }

    ordered_results = sorted(
        results.values(),
        key=lambda r: (str(r.source_asset), [p.key for p in cfg.platforms].index(r.platform)),
    )
    for result in ordered_results:
        if result.size > cfg.max_asset_bytes:
            raise PackagingError(
                f"{result.path.name} is {result.size} bytes, over the "
                f"{cfg.max_asset_bytes} byte GitHub asset limit"
            )

    expected_names = {r.path.name for r in ordered_results}
    expected_names.update(name for name, _ in original_files)
    expected_names.add(STATS_ASSET)
    baseline, retired = prepare_baseline(
        existing_block,
        (existing or {}).get("assets") or [],
        expected_names,
    )
    stats = build_stats(
        cfg,
        release,
        existing,
        existing_block,
        baseline=baseline,
        retired=retired,
        replacing=True,
        expected_names=expected_names,
    )
    stats_path = work / tag / STATS_ASSET
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(stats.text, encoding="utf-8")

    block = {
        "v": BUILDER_VERSION,
        "fingerprint": release_fingerprint(cfg, release, updater),
        "built_at": utcnow(),
        "updater": {
            key: {"tag": info.get("tag"), "name": info.get("name"), "size": info.get("size")}
            for key, info in sorted(updater.items())
        },
        "assets": [
            {
                "name": r.path.name,
                "source_asset": r.source_asset,
                "platform": r.platform,
                "size": r.size,
                "sha256": r.sha256,
            }
            for r in ordered_results
        ],
        "original_assets": [name for name, _ in original_files],
        "skipped": skipped_assets,
        "stats": {
            "baseline": stats.baseline,
            "retired": stats.retired,
            "generated_at": stats.generated_at,
        },
        "failures": failures,
    }
    title = release_title(cfg, release)
    notes = render_notes(cfg, release, block, cfg.mirror_repo or "(mirror)")
    notes_path = work / tag / "notes.md"
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(notes, encoding="utf-8")

    action = "built-dry-run" if dry_run else "built"
    if dry_run:
        log(f"  dry run: built {len(ordered_results)} package(s); nothing uploaded")
    else:
        if not cfg.mirror_repo:
            raise SystemExit("mirror_repo is required to publish; set it in mirror.config.json")
        gh.ensure_release(cfg.mirror_repo, tag, title, notes_path, log=log)
        uploads = [r.path for r in ordered_results] + [path for _name, path in original_files]
        uploads.append(stats_path)
        gh.upload(cfg.mirror_repo, tag, uploads, log=log)
        log(f"  published {len(ordered_results)} portable package(s) for {tag}")

    return {
        "tag": tag,
        "action": action,
        "title": title,
        "assets": [r.path.name for r in ordered_results],
        "original_assets": [name for name, _ in original_files],
        "stats": stats.totals,
        "block": block,
        "failures": failures,
    }


def refresh_release_stats(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    existing: Mapping[str, Any],
    block: Optional[Mapping[str, Any]],
    gh: GitHub,
    work: Path,
    log: Log,
    dry_run: bool,
) -> StatsReport:
    """Regenerate stats.txt for a release whose packages are current."""

    expected = set()
    if block:
        expected.update(str(a.get("name")) for a in block.get("assets") or [])
        expected.update(str(name) for name in block.get("original_assets") or [])
    stats = build_stats(
        cfg,
        release,
        existing,
        block,
        replacing=False,
        expected_names=expected,
    )
    tag = release_tag(release)
    path = work / "stats" / tag / STATS_ASSET
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stats.text, encoding="utf-8")
    if dry_run:
        log(f"  dry run: generated {STATS_ASSET} (not uploaded)")
    else:
        gh.upload(cfg.mirror_repo, tag, [path], log=log)
        log(f"  updated {STATS_ASSET}")
    return stats


def sync_source_readme(
    cfg: MirrorConfig,
    gh: GitHub,
    work: Path,
    log: Log,
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    """Copy the source repository's README into the mirror repository.

    The default target is ``README.md``, so the mirror's landing page matches
    the upstream project.  Set ``source_readme.path`` to another file when the
    mirror README is maintained separately.
    """

    if not cfg.readme_enabled:
        log("  source README sync disabled")
        return None
    source = gh.get_readme(cfg.source_repo)
    if not source or not source.get("text"):
        log("  warning: source repository has no README to copy")
        return None

    text = str(source["text"])
    if not text.endswith("\n"):
        text += "\n"
    local = work / "readme" / cfg.readme_path
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(text, encoding="utf-8")

    info: Dict[str, Any] = {
        "path": cfg.readme_path,
        "source_path": source.get("path"),
        "source_name": source.get("name"),
        "source_url": source.get("html_url"),
        "url": None,
        "action": "dry-run" if dry_run else "pending",
    }
    if cfg.mirror_repo:
        info["url"] = (
            f"https://raw.githubusercontent.com/{cfg.mirror_repo}/"
            f"{quote(cfg.branch, safe='')}/{quote(cfg.readme_path, safe='/')}"
        )
    if dry_run:
        log(f"  dry run: copied {source.get('name')} to {cfg.readme_path} (not uploaded)")
        return info
    if not cfg.mirror_repo:
        log("  mirror_repo is not set; README was not uploaded")
        info["action"] = "not-uploaded"
        return info

    changed = gh.put_file(
        cfg.mirror_repo,
        cfg.readme_path,
        text,
        cfg.readme_commit_message,
        cfg.branch,
    )
    info["action"] = "updated" if changed else "unchanged"
    log(f"  source README {info['action']}: {cfg.readme_path}")
    return info


def sync(args) -> int:
    log = Log(args.verbose)
    cfg = load_config(
        args.config,
        source_repo=args.source_repo,
        mirror_repo=args.mirror_repo,
        platforms=parse_csv(args.platforms) or None,
    )
    log(f"config: {cfg.describe()}")
    if not cfg.mirror_repo and not args.dry_run:
        raise SystemExit(
            "mirror_repo is not set. Add it to mirror.config.json or pass --mirror-repo."
        )
    gh = GitHub()
    if not args.dry_run and not gh.gh_available():
        raise SystemExit(
            "The GitHub CLI (gh) is required for publishing. "
            "It is preinstalled on GitHub Actions runners; install it locally "
            "from https://cli.github.com/."
        )

    updater = resolve_updater_assets(cfg, gh, log)
    releases = filter_source_releases(cfg, gh.list_releases(cfg.source_repo), args)
    log(
        f"mirroring {len(releases)} release(s) from {cfg.source_repo} "
        f"into {cfg.mirror_repo or '(dry run)'}"
    )

    work_root = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="pfp-"))
    work_root.mkdir(parents=True, exist_ok=True)
    index_path = Path(args.index) if args.index else Path("index.json")
    previous_index = None
    if index_path.is_file():
        try:
            previous_index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous_index = None

    report: Dict[str, Any] = {
        "started_at": utcnow(),
        "source_repo": cfg.source_repo,
        "mirror_repo": cfg.mirror_repo,
        "updater": updater,
        "releases": [],
        "failures": [],
    }
    index_updates: Dict[str, Optional[Mapping[str, Any]]] = {}
    readme_info: Optional[Dict[str, Any]] = None

    # Download a portable app only when a release actually needs it.  A run
    # where every release is current then costs API calls only.
    updater_paths: Dict[str, Path] = {}
    updater_lock = threading.Lock()

    def ensure_updater(key: str) -> Path:
        with updater_lock:
            if key not in updater_paths:
                info = updater[key]
                log(f"  preparing updater for {key}")
                updater_paths[key] = gh.download(
                    cfg.updater_repo,
                    {
                        "name": info["name"],
                        "size": info["size"],
                        "id": info.get("id"),
                        "browser_download_url": info.get("url"),
                    },
                    work_root / "updater" / str(info["tag"]) / str(info["name"]),
                    log=log,
                )
            return updater_paths[key]

    try:
        try:
            readme_info = sync_source_readme(
                cfg, gh, work_root, log, args.dry_run
            )
        except (GitHubError, ApiError, OSError) as exc:
            log(f"  warning: could not mirror source README: {exc}")
            report["failures"].append({"tag": "(readme)", "error": str(exc)})
        report["source_readme"] = readme_info

        # Oldest first so the newest release is created last and is what GitHub
        # shows as the latest release.
        for release in reversed(releases):
            tag = release_tag(release)
            log(f"release {tag}")
            existing = None
            if cfg.mirror_repo:
                existing = gh.get_release_by_tag(cfg.mirror_repo, tag)
            fingerprint = release_fingerprint(cfg, release, updater)
            block = parse_block(existing.get("body")) if existing else None
            if not args.force and block_is_current(cfg, release, existing, block, fingerprint):
                log(f"  up to date ({len(block.get('assets') or [])} package(s))")
                stats = None
                try:
                    stats = refresh_release_stats(
                        cfg, release, existing, block, gh, work_root, log, args.dry_run
                    )
                except (GitHubError, ApiError, OSError) as exc:
                    log(f"  warning: could not update {STATS_ASSET}: {exc}")
                    report["failures"].append(
                        {"tag": tag, "error": f"{STATS_ASSET}: {exc}"}
                    )
                index_updates[tag] = build_index_entry(cfg, release, block)
                report["releases"].append(
                    {
                        "tag": tag,
                        "action": "up-to-date",
                        "stats": stats.totals if stats else {},
                    }
                )
                continue

            try:
                result = build_for_release(
                    cfg,
                    release,
                    existing,
                    block,
                    gh,
                    updater,
                    ensure_updater,
                    work_root,
                    log,
                    dry_run=args.dry_run,
                )
            except (GitHubError, ApiError, PackagingError, OSError) as exc:
                log(f"  FAILED: {exc}")
                log.debug(traceback.format_exc())
                report["failures"].append({"tag": tag, "error": str(exc)})
                report["releases"].append({"tag": tag, "action": "failed", "error": str(exc)})
                continue

            if not result:
                continue
            report["releases"].append({k: v for k, v in result.items() if k != "block"})
            if result["action"] == "built" and result.get("block"):
                index_updates[tag] = build_index_entry(cfg, release, result["block"])
                if result.get("failures"):
                    report["failures"].append(
                        {"tag": tag, "error": "; ".join(result["failures"])}
                    )
            elif result["action"] == "failed":
                # A release whose packages could not be built must fail the
                # run; otherwise the workflow reports success having published
                # nothing for that tag.
                report["failures"].append(
                    {"tag": tag, "error": str(result.get("reason") or "packaging failed")}
                )
            elif result["action"] == "skipped":
                index_updates[tag] = None

            if args.keep_work:
                log(f"  work kept at {work_root / tag}")
            else:
                shutil.rmtree(work_root / tag, ignore_errors=True)

        if not args.skip_index and not args.dry_run:
            index = make_index(cfg, index_updates, previous_index, readme_info)
            write_index(cfg, index, index_path, log)
    finally:
        if args.keep_work:
            log(f"work directory: {work_root}")
        elif not args.work_dir:
            shutil.rmtree(work_root, ignore_errors=True)

    report["finished_at"] = utcnow()
    report["failed"] = bool(report["failures"])
    report_path = Path(args.json_report) if args.json_report else Path("mirror-report.json")
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_summary(report, log)
    return 1 if report["failures"] else 0


def write_summary(report: Mapping[str, Any], log: Log) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Portable flashing packages",
        "",
        f"Source: `{report['source_repo']}` → mirror: `{report['mirror_repo'] or '(dry run)'}`",
        "",
        "| Release | Result |",
        "| --- | --- |",
    ]
    for item in report.get("releases", []):
        lines.append(f"| `{item.get('tag')}` | {item.get('action')} |")
    if report.get("failures"):
        lines += ["", "### Failures", ""]
        for failure in report["failures"]:
            lines.append(f"- `{failure.get('tag')}`: {failure.get('error')}")
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        log(f"could not write step summary: {exc}")


def build_only(args) -> int:
    log = Log(args.verbose)
    cfg = load_config(
        args.config,
        source_repo=args.source_repo,
        mirror_repo=args.mirror_repo,
        platforms=parse_csv(args.platforms) or None,
    )
    gh = GitHub()
    tag = args.tag
    if not tag:
        raise SystemExit("build requires --tag (for example --tag 3.1.2)")
    release = gh.get_release_by_tag(cfg.source_repo, tag)
    if not release:
        raise SystemExit(f"{cfg.source_repo} has no release tagged {tag}")
    updater = resolve_updater_assets(cfg, gh, log)
    out_dir = Path(args.out or f"dist/{tag}")
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="pfp-build-"))
    work.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {"tag": tag, "source": cfg.source_repo, "outputs": []}
    try:
        updater_paths = download_updater_files(cfg, gh, updater, work, log)
        candidates = select_firmware_assets(release.get("assets") or [], cfg)
        for asset in candidates:
            path = download_release_asset(cfg, gh, asset, work / "src", log)
            package = inspect_firmware(path)
            if cfg.require_scatter and not package.scatters:
                log(f"skipping {asset['name']}: no scatter")
                continue
            meta = resolve_metadata(cfg, str(asset["name"]))
            ini_text = fw_package_ini(
                meta["software_name"],
                release_version(tag),
                meta["target_model"],
                meta["target_type"],
                meta["description"] or str(release.get("name") or ""),
            )
            for platform in cfg.platforms:
                if platform.key not in updater_paths:
                    continue
                out_path = out_dir / output_name(str(asset["name"]), platform)
                result = build_portable_package(
                    updater_paths[platform.key],
                    package,
                    platform,
                    out_path,
                    ini_text,
                    meta["software_name"],
                    release_version(tag),
                    str(asset["name"]),
                    log=log,
                )
                manifest["outputs"].append(
                    {
                        "name": result.path.name,
                        "source_asset": result.source_asset,
                        "platform": result.platform,
                        "size": result.size,
                        "sha256": result.sha256,
                    }
                )
        stats = build_stats(cfg, release, None, None, replacing=False)
        (out_dir / STATS_ASSET).write_text(stats.text, encoding="utf-8")
        manifest["stats"] = stats.totals
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    if not manifest["outputs"]:
        log("nothing was built")
        return 1
    log(f"built {len(manifest['outputs'])} package(s) in {out_dir}")
    return 0


def status(args) -> int:
    log = Log(args.verbose)
    cfg = load_config(
        args.config,
        source_repo=args.source_repo,
        mirror_repo=args.mirror_repo,
        platforms=parse_csv(args.platforms) or None,
    )
    gh = GitHub()
    updater = resolve_updater_assets(cfg, gh, log)
    releases = filter_source_releases(cfg, gh.list_releases(cfg.source_repo), args)
    for release in releases:
        tag = release_tag(release)
        existing = gh.get_release_by_tag(cfg.mirror_repo, tag) if cfg.mirror_repo else None
        block = parse_block(existing.get("body")) if existing else None
        fingerprint = release_fingerprint(cfg, release, updater)
        state = "missing"
        if existing:
            state = "current" if block_is_current(cfg, release, existing, block, fingerprint) else "stale"
        print(f"{tag:<32} {state}")
    return 0


def _common_options() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="mirror.config.json")
    common.add_argument("--source-repo", help="override source_repo")
    common.add_argument("--mirror-repo", help="override mirror_repo")
    common.add_argument("--platforms", help="comma-separated platform keys")
    common.add_argument("--tags", help="comma-separated tag globs to include")
    common.add_argument("--limit", type=int, help="only consider the N newest releases")
    common.add_argument(
        "--offset", type=int, default=0, help="skip the N newest releases (batch backfill)"
    )
    common.add_argument("--force", action="store_true", help="rebuild even when up to date")
    common.add_argument("--dry-run", action="store_true", help="build but do not upload")
    common.add_argument("--work-dir", help="reuse a working directory")
    common.add_argument("--keep-work", action="store_true", help="keep downloaded/build files")
    common.add_argument("--index", default="index.json", help="path to the public index")
    common.add_argument("--skip-index", action="store_true")
    common.add_argument("--json-report", default="mirror-report.json")
    common.add_argument("--verbose", action="store_true")
    return common


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror firmware releases as portable flashing packages."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = _common_options()
    sub.add_parser("sync", parents=[common], help="build and publish every release")
    build_cmd = sub.add_parser(
        "build", parents=[common], help="build one release locally, without uploading"
    )
    build_cmd.add_argument("--tag", required=True)
    build_cmd.add_argument("--out", help="output directory (default dist/<tag>)")
    sub.add_parser("status", parents=[common], help="show which mirror releases are current")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return build_only(args)
        if args.command == "status":
            return status(args)
        return sync(args)
    except (ConfigError, GitHubError, ApiError, PackagingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
