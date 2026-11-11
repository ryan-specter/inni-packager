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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import postinstall
import release_notes
import repos as repo_fallback
from gh import ApiError, GitHub, GitHubError
from manifest import post_installs_for_source
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
    strip_block,
)
from packager import (
    BuildResult,
    PackagingError,
    build_portable_package,
    inspect_firmware,
    repack_firmware_zip,
)
from stats import STATS_ASSET, StatsReport, build_stats, parse_stats_text, prepare_baseline

INDEX_SCHEMA = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Log:
    def __init__(
        self, verbose: bool = False, sink: Optional[Callable[[str], None]] = None
    ) -> None:
        self.verbose = verbose
        self.sink = sink
        self.lines: List[str] = []
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.lines.append(message)
            print(f"[{stamp}] {message}", flush=True)
            if self.sink is not None:
                self.sink(f"[{stamp}] {message}")

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
    wanted = {platform.updater_asset.lower()}
    wanted.update(name.lower() for name in platform.alt_assets)
    for release in releases:
        if release.get("draft"):
            continue
        tag = release_tag(release)
        if platform.pinned_tag and tag != platform.pinned_tag:
            continue
        for asset in release.get("assets") or []:
            if str(asset.get("name") or "").lower() in wanted:
                return release, asset
    return None


def resolve_updater_assets(
    cfg: MirrorConfig, gh: GitHub, log: Log
) -> Dict[str, Dict[str, Any]]:
    releases_by_repo: Dict[str, Sequence[Mapping[str, Any]]] = {}
    chosen: Dict[str, Dict[str, Any]] = {}
    for platform in cfg.platforms:
        repo = platform.updater_repo or cfg.updater_repo
        if repo not in releases_by_repo:
            releases_by_repo[repo] = gh.list_releases(repo)
        found = find_updater_asset(releases_by_repo[repo], platform)
        if not found:
            if platform.pinned_tag:
                raise SystemExit(
                    f"updater {repo} has no {platform.updater_asset} "
                    f"in pinned release {platform.pinned_tag}"
                )
            if platform.optional:
                log(f"  skipping {platform.key}: no {platform.updater_asset} published yet")
                continue
            names = ", ".join((platform.updater_asset,) + platform.alt_assets)
            raise SystemExit(
                f"updater {repo} has no asset named {names}; "
                f"cannot build {platform.key}"
            )
        release, asset = found
        chosen[platform.key] = {
            "repo": repo,
            "tag": release_tag(release),
            "name": str(asset["name"]),
            "size": int(asset.get("size") or 0),
            "id": asset.get("id"),
            "digest": str(asset.get("digest") or ""),
            "updated_at": str(asset.get("updated_at") or ""),
            "url": asset.get("browser_download_url"),
        }
        log(
            f"  updater {platform.key}: {asset['name']} from "
            f"{repo}@{release_tag(release)}"
        )
        if platform.driver_asset:
            driver = next(
                (
                    candidate
                    for candidate in release.get("assets") or []
                    if str(candidate.get("name") or "").lower()
                    == platform.driver_asset.lower()
                ),
                None,
            )
            if not driver:
                raise SystemExit(
                    f"updater {repo}@{release_tag(release)} has no "
                    f"{platform.driver_asset}; cannot build {platform.key}"
                )
            chosen[platform.key]["driver"] = {
                "repo": repo,
                "tag": release_tag(release),
                "name": str(driver["name"]),
                "size": int(driver.get("size") or 0),
                "id": driver.get("id"),
                "digest": str(driver.get("digest") or ""),
                "updated_at": str(driver.get("updated_at") or ""),
                "url": driver.get("browser_download_url"),
            }
            log(
                f"  driver {platform.key}: {driver['name']} from "
                f"{repo}@{release_tag(release)}"
            )
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
    shard = parse_shard(getattr(args, "shard", ""))
    if shard:
        index, total = shard
        # Splitting a long backfill across parallel jobs keeps every job under
        # the Actions time limit.  The slice is stable for a given release list.
        selected = [release for position, release in enumerate(selected) if position % total == index]
    return selected


def parse_shard(value: Optional[str]) -> Optional[Tuple[int, int]]:
    """Parse the ``INDEX/TOTAL`` form accepted by ``--shard``."""

    if not value:
        return None
    text = str(value).strip()
    index_text, _, total_text = text.partition("/")
    try:
        index = int(index_text)
        total = int(total_text)
    except ValueError as exc:
        raise ConfigError(f"--shard must look like 0/4, not {value!r}") from exc
    if total < 1 or not 0 <= index < total:
        raise ConfigError(f"--shard {value!r} needs 0 <= index < total")
    return index, total


def mirror_release_as_source(
    cfg: MirrorConfig, release: Mapping[str, Any]
) -> Dict[str, Any]:
    """Present a mirror release the way its source release looked."""

    tag = release_tag(release)
    title = str(release.get("name") or "").strip()
    if cfg.title_prefix and title.lower().startswith(cfg.title_prefix.lower()):
        title = title[len(cfg.title_prefix) :].strip()
    return {
        "tag_name": tag,
        "name": title or tag,
        "body": release_notes.strip_generated_header(release.get("body")),
        "published_at": release.get("published_at"),
        "prerelease": bool(release.get("prerelease")),
        "assets": list(release.get("assets") or []),
    }


def mirror_source_releases(
    cfg: MirrorConfig, gh: GitHub, log: Log
) -> List[Dict[str, Any]]:
    """Releases that can be re-packed from the mirror's own stored firmware.

    Upstream projects sometimes delete or replace the releases a mirror was
    built from, which would leave the stored firmware zip stranded beside
    packages built by an old updater.  Those original assets are still on the
    mirror, so the packages can be rebuilt from them without touching the
    source repository.
    """

    releases: List[Dict[str, Any]] = []
    for release in gh.list_releases(cfg.mirror_repo):
        tag = release_tag(release)
        block = parse_block(release.get("body"))
        if not block:
            log(f"  skipping {tag}: no build record")
            continue
        names = {str(name) for name in block.get("original_assets") or []}
        assets = [
            asset
            for asset in release.get("assets") or []
            if str(asset.get("name") or "") in names
        ]
        if not assets:
            log(f"  skipping {tag}: no stored firmware assets")
            continue
        item = mirror_release_as_source(cfg, release)
        item["assets"] = assets
        releases.append(item)
    return releases


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
        str(a.get("name")): a
        for a in existing.get("assets") or []
        if a.get("state", "uploaded") == "uploaded"
    }
    for item in assets:
        name = str(item.get("name") or "")
        size = int(item.get("size") or 0)
        live = current.get(name)
        if not name or live is None or int(live.get("size") or 0) != size:
            return False
        # When GitHub reports a digest, a same-sized asset with different
        # bytes still means the published package is stale.
        recorded = str(item.get("sha256") or "").lower()
        digest = str(live.get("digest") or "").lower()
        if recorded and digest and digest.rsplit(":", 1)[-1] != recorded:
            return False
    button_assets = block.get("button_assets")
    if not isinstance(button_assets, list) or not button_assets:
        return False
    # Original assets are part of the published result too.  A missing copy or
    # one whose size no longer matches the build record means the release is
    # not fresh and must be repacked.
    original_sizes = block.get("original_asset_sizes")
    for item in block.get("original_assets") or []:
        name = str(item)
        live = current.get(name)
        if live is None:
            return False
        expected = (
            original_sizes.get(name)
            if isinstance(original_sizes, dict)
            else None
        )
        if expected is not None and int(live.get("size") or 0) != int(expected):
            return False
    # The SVGs live in the repository (not as release assets), because GitHub
    # serves release downloads as application/octet-stream.  Their presence is
    # published by sync before any release notes are rendered.
    return True


def assets_differ(source: Mapping[str, Any], mirror: Mapping[str, Any]) -> bool:
    """Compare two release assets using digests when both sides have them."""

    if int(source.get("size") or 0) != int(mirror.get("size") or 0):
        return True
    left = str(source.get("digest") or "")
    right = str(mirror.get("digest") or "")
    return bool(left and right and left != right)


def review_release(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    existing: Optional[Mapping[str, Any]],
    updater: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Classify one source release against its mirror without downloading."""

    tag = release_tag(release)
    item: Dict[str, Any] = {
        "tag": tag,
        "state": "current",
        "missing": [],
        "changed": [],
    }
    if not existing:
        item["state"] = "missing-release"
        return item

    block = parse_block(existing.get("body"))
    mirror_assets = {
        str(asset.get("name")): asset for asset in existing.get("assets") or []
    }
    recorded_originals = {
        str(name) for name in (block or {}).get("original_assets") or []
    }
    original_sizes = (block or {}).get("original_asset_sizes")
    if not isinstance(original_sizes, dict):
        original_sizes = {}
    if cfg.copy_original_assets:
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if not name or name == STATS_ASSET:
                continue
            if int(asset.get("size") or 0) > cfg.max_asset_bytes:
                continue
            current = mirror_assets.get(name)
            if not current:
                item["missing"].append(name)
            elif name in recorded_originals:
                # A mirrored original is a repacked copy, so its digest can
                # never equal the source asset's digest.  Compare its size to
                # the size recorded by the build instead.
                expected = original_sizes.get(name)
                if expected is None or int(current.get("size") or 0) != int(expected):
                    item["changed"].append(name)
            elif assets_differ(asset, current):
                item["changed"].append(name)

    fingerprint = release_fingerprint(cfg, release, updater)
    if not block_is_current(cfg, release, existing, block, fingerprint):
        item["state"] = "needs-build"
    elif item["missing"]:
        item["state"] = "missing-assets"
    elif item["changed"]:
        item["state"] = "changed-assets"
    return item


def review_mirror(
    cfg: MirrorConfig, gh: GitHub, args, log: Optional[Log] = None
) -> Dict[str, Any]:
    """Review every selected source release against the mirror repository."""

    active_log = log or Log()
    updater = resolve_updater_assets(cfg, gh, active_log)
    # sync adds the manifest's post-install links to the config before it
    # fingerprints a build.  Do the same here, or every release that has one
    # looks stale to review/status even though it was just built.
    resolve_post_install_for(cfg, gh, active_log)
    releases = filter_source_releases(cfg, gh.list_releases(cfg.source_repo), args)
    rows: List[Dict[str, Any]] = []
    for release in releases:
        existing = (
            gh.get_release_by_tag(cfg.mirror_repo, release_tag(release))
            if cfg.mirror_repo
            else None
        )
        rows.append(review_release(cfg, release, existing, updater))
    needing = [row for row in rows if row["state"] != "current"]
    return {
        "source_repo": cfg.source_repo,
        "mirror_repo": cfg.mirror_repo,
        "releases": rows,
        "summary": {
            "total": len(rows),
            "current": len(rows) - len(needing),
            "needing_update": len(needing),
            "missing_releases": sum(
                1 for row in rows if row["state"] == "missing-release"
            ),
        },
    }


def review(args) -> int:
    log = Log(args.verbose)
    cfg = load_config(
        args.config,
        source_repo=args.source_repo,
        mirror_repo=args.mirror_repo,
        platforms=parse_csv(args.platforms) or None,
    )
    gh = GitHub()
    report = review_mirror(cfg, gh, args, log)
    text = json.dumps(report, indent=2) + "\n"
    report_path = Path(args.json_report)
    report_path.write_text(text, encoding="utf-8")
    if args.json:
        print(text)
    else:
        print(f"{'release':<32} state")
        for row in report["releases"]:
            extra = ""
            if row["missing"]:
                extra += " missing=" + ",".join(row["missing"])
            if row["changed"]:
                extra += " changed=" + ",".join(row["changed"])
            print(f"{row['tag']:<32} {row['state']}{extra}")
        summary = report["summary"]
        print(
            f"\n{summary['needing_update']}/{summary['total']} release(s) "
            f"need an update; report: {report_path}"
        )
    return 0


def build_index_entry(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    block: Mapping[str, Any],
) -> Dict[str, Any]:
    tag = release_tag(release)
    tag_q = quote(tag, safe="")
    fallback_repo = repo_fallback.fallback_repo(cfg.mirror_repo)
    assets = []
    for item in block.get("assets") or []:
        name = str(item.get("name") or "")
        asset = {
            "name": name,
            "source_asset": item.get("source_asset"),
            "platform": item.get("platform"),
            "model": item.get("model"),
            "type": item.get("type"),
            "size": int(item.get("size") or 0),
            "sha256": item.get("sha256"),
            "url": f"https://github.com/{cfg.mirror_repo}/releases/download/"
            f"{tag_q}/{quote(name, safe='')}",
        }
        if fallback_repo:
            asset["fallback_url"] = (
                f"https://github.com/{fallback_repo}/releases/download/"
                f"{tag_q}/{quote(name, safe='')}"
            )
        assets.append(asset)
    return {
        "tag": tag,
        "title": release_title(cfg, release),
        "source_title": str(release.get("name") or "").strip() or tag,
        "source_url": f"https://github.com/{cfg.source_repo}/releases/tag/{tag_q}",
        "source_fallback_url": (
            f"https://github.com/{repo_fallback.fallback_repo(cfg.source_repo)}/releases/tag/{tag_q}"
            if repo_fallback.fallback_repo(cfg.source_repo) else ""
        ),
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
    fallback_repo = repo_fallback.fallback_repo(cfg.mirror_repo)
    index = {
        "schema": INDEX_SCHEMA,
        "generated_at": utcnow(),
        "source_repo": cfg.source_repo,
        "source_fallback_repo": repo_fallback.fallback_repo(cfg.source_repo),
        "mirror_repo": cfg.mirror_repo,
        "mirror_fallback_repo": fallback_repo,
        "fallback_index_url": (
            f"https://raw.githubusercontent.com/{fallback_repo}/"
            f"{quote(cfg.branch, safe='')}/index.json" if fallback_repo else ""
        ),
        "title_prefix": cfg.title_prefix,
        "updater_repo": cfg.updater_repo,
        "platforms": [
            {
                "key": p.key,
                "os": p.os,
                "arch": p.arch,
                "suffix": p.suffix,
                "layout": p.layout,
                "legacy": p.legacy,
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


def publish_index(
    cfg: MirrorConfig, gh: GitHub, index: Mapping[str, Any], log: Log
) -> bool:
    """Commit index.json to the mirror repository.

    CI checks the mirror out and commits the file itself.  A local backfill has
    no checkout, so it publishes the same content through the contents API.
    """

    if not cfg.mirror_repo:
        log("  mirror_repo is not set; index was not published")
        return False
    text = json.dumps(index, indent=2, sort_keys=False) + "\n"
    changed = gh.put_file(
        cfg.mirror_repo,
        "index.json",
        text,
        "Update portable package index",
        cfg.branch,
    )
    log(f"  index {'published' if changed else 'unchanged on GitHub'}")
    return changed


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
        paths[key] = gh.download(str(info.get("repo") or cfg.updater_repo), asset, dest, log=log)
        driver = info.get("driver")
        if isinstance(driver, Mapping):
            driver_dest = work / "updater" / str(driver["tag"]) / str(driver["name"])
            driver_asset = {
                "name": driver["name"],
                "size": driver["size"],
                "id": driver.get("id"),
                "browser_download_url": driver.get("url"),
            }
            log(f"  preparing driver for {key}")
            paths[key + "::driver"] = gh.download(
                str(driver.get("repo") or cfg.updater_repo),
                driver_asset,
                driver_dest,
                log=log,
            )
    return paths


def resolve_post_install_for(
    cfg: MirrorConfig, gh: GitHub, log: Log
) -> Optional[postinstall.PostInstall]:
    """Use configured links, falling back to the live catalog manifest."""

    urls = list(cfg.post_installs)
    if not urls:
        try:
            urls = post_installs_for_source(cfg.source_repo)
        except (OSError, ValueError) as exc:
            log(f"  warning: could not read post_install from manifest: {exc}")
    if urls and list(cfg.post_installs) != urls:
        cfg.post_installs = urls
        log(f"  post-install: {', '.join(urls)}")
    return postinstall.resolve(urls, gh, log=log)


def download_release_asset(
    cfg: MirrorConfig, gh: GitHub, asset: Mapping[str, Any], directory: Path, log: Log
) -> Path:
    name = str(asset["name"])
    dest = directory / name
    if dest.exists() and dest.stat().st_size == int(asset.get("size") or 0):
        return dest
    return gh.download(cfg.source_repo, asset, dest, log=log)


def read_published_stats(
    cfg: MirrorConfig,
    existing: Optional[Mapping[str, Any]],
    gh: GitHub,
    work: Path,
    tag: str,
    log: Log,
) -> Optional[Dict[str, Any]]:
    """Read the previous stats.txt before a rebuild replaces it.

    GitHub resets an asset's counter when ``--clobber`` uploads it again.  The
    hidden build block normally carries the baseline, but older releases were
    built before that ledger existed; their published stats.txt is the only
    record of earlier downloads.  Parsing it lets the next statistics file keep
    those cumulative totals instead of starting from the live counters.
    """

    if not cfg.mirror_repo or not existing:
        return None
    asset = next(
        (
            item
            for item in existing.get("assets") or []
            if str(item.get("name") or "") == STATS_ASSET
        ),
        None,
    )
    if not asset:
        return None
    dest = work / "stats" / tag / "previous-stats.txt"
    try:
        path = gh.download(cfg.mirror_repo, asset, dest, log=log)
        return parse_stats_text(path.read_text(encoding="utf-8", errors="replace"))
    except (GitHubError, ApiError, OSError, ValueError) as exc:
        log(f"  warning: could not read previous {STATS_ASSET}: {exc}")
        return None


def build_for_release(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    existing: Optional[Mapping[str, Any]],
    existing_block: Optional[Mapping[str, Any]],
    gh: GitHub,
    updater: Mapping[str, Mapping[str, Any]],
    post_install: Optional[postinstall.PostInstall],
    ensure_updater: Callable[[str], Path],
    work: Path,
    log: Log,
    dry_run: bool = False,
    copy_originals: bool = True,
    keep_stats: bool = False,
    ensure_driver: Optional[Callable[[str], Optional[Path]]] = None,
) -> Optional[Dict[str, Any]]:
    """Download, repackage and upload one release.  Returns a report entry."""

    tag = release_tag(release)
    version = release_version(tag, str(release.get("name") or ""))
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
            package = inspect_firmware(
                path,
                source_repo=cfg.source_repo,
                preferred_scatter=cfg.scatter,
            )
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
    original_asset_sizes: Dict[str, int] = {}
    original_names: List[str] = []
    if cfg.copy_original_assets and copy_originals:
        original_dir = work / tag / "original"
        original_src = original_dir / "src"
        original_out = original_dir / "out"
        downloaded = {str(asset["name"]): path for asset, path, _ in firmware}
        for asset in source_assets:
            name = str(asset.get("name") or "")
            if not name or name == STATS_ASSET:
                continue
            size = int(asset.get("size") or 0)
            if size > cfg.max_asset_bytes:
                log(f"  not copying original {name}: larger than max_asset_bytes")
                continue
            source_path = downloaded.get(name)
            if source_path is None:
                try:
                    source_path = download_release_asset(
                        cfg, gh, asset, original_src, log
                    )
                except (GitHubError, ApiError) as exc:
                    log(f"  warning: could not copy original asset {name}: {exc}")
                    continue
            upload_path = source_path
            # Stock zips get the same maximum deflate as the portable
            # packages.  Only alternate scatter layouts are dropped, and only
            # when a standard *_Android_scatter.txt survives.
            if name.lower().endswith(".zip"):
                try:
                    repacked = repack_firmware_zip(
                        source_path, original_out / name, log=log
                    )
                except PackagingError as exc:
                    log(f"  warning: could not repack original {name}: {exc}")
                else:
                    upload_path = repacked.path
            original_files.append((name, upload_path))
            original_asset_sizes[name] = upload_path.stat().st_size
        original_names = [name for name, _ in original_files]
    else:
        # Re-packing an existing mirror release: the firmware inputs are the
        # originals already stored on the mirror, so they are neither
        # downloaded again nor re-uploaded.  Keep the recorded names so the
        # build record and the index still describe them.
        original_names = [
            str(name) for name in (existing_block or {}).get("original_assets") or []
        ]
        recorded = (existing_block or {}).get("original_asset_sizes")
        original_asset_sizes = dict(recorded) if isinstance(recorded, dict) else {}

    out_dir = work / tag / "out"
    tasks = []
    meta_by_source: Dict[str, Dict[str, str]] = {}
    for asset, _path, package in firmware:
        meta_by_source[str(asset["name"])] = resolve_metadata(
            cfg, str(asset["name"])
        )
    for asset, _path, package in firmware:
        meta = meta_by_source[str(asset["name"])]
        ini_text = fw_package_ini(
            software_name=meta["software_name"],
            version=version,
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
        post = post_install.render(platform.key) if post_install else None
        post_name, post_data = post if post else ("", b"")
        return build_portable_package(
            ensure_updater(platform.key),
            package,
            platform,
            out_path,
            ini_text,
            meta["software_name"],
            version,
            str(asset["name"]),
            log=log,
            source_repo=cfg.source_repo,
            post_install_name=post_name,
            post_install_data=post_data,
            target_model=meta["target_model"],
            target_type=meta["target_type"],
            driver_path=ensure_driver(platform.key) if ensure_driver else None,
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
        # A source archive can be intentionally unsupported (for example, a
        # vendor zip with no scatter, or one with two alternate scatters).
        # Do not fail the whole mirror and prevent later releases' indexes from
        # being committed; record it as skipped with the reason instead.
        return {
            "tag": tag,
            "action": "skipped",
            "reason": "; ".join(failures) or "packaging failed",
            "failures": failures,
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
    expected_names.update(original_names)
    expected_names.add(STATS_ASSET)
    stats: Optional[StatsReport] = None
    stats_path: Optional[Path] = None
    if keep_stats:
        # A repack rebuilds the packages, not the download history: keep the
        # published stats.txt and its ledger exactly as they are.
        log(f"  keeping {STATS_ASSET} as published")
    else:
        previous_stats = read_published_stats(cfg, existing, gh, work, tag, log)
        baseline, retired = prepare_baseline(
            existing_block,
            (existing or {}).get("assets") or [],
            expected_names,
            previous_stats=previous_stats,
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
            previous_stats=previous_stats,
        )
        stats_path = work / tag / STATS_ASSET
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(stats.text, encoding="utf-8")

    used_platforms = [r.platform for r in ordered_results]
    button_paths = release_notes.write_button_assets(
        work / tag / "buttons", used_platforms
    )
    button_entries = [
        {"name": path.name, "size": path.stat().st_size} for path in button_paths
    ]

    block = {
        "v": BUILDER_VERSION,
        "fingerprint": release_fingerprint(cfg, release, updater),
        "built_at": utcnow(),
        "updater": {
            key: {
                "tag": info.get("tag"),
                "name": info.get("name"),
                "size": info.get("size"),
                "driver": info.get("driver"),
            }
            for key, info in sorted(updater.items())
        },
        "assets": [
            {
                "name": r.path.name,
                "source_asset": r.source_asset,
                "platform": r.platform,
                "model": meta_by_source.get(r.source_asset, {}).get("target_model", ""),
                "type": meta_by_source.get(r.source_asset, {}).get("target_type", ""),
                "size": r.size,
                "sha256": r.sha256,
            }
            for r in ordered_results
        ],
        "button_assets": button_entries,
        "original_assets": original_names,
        "original_asset_sizes": original_asset_sizes,
        "skipped": skipped_assets,
        "stats": (
            {
                "baseline": stats.baseline,
                "retired": stats.retired,
                "generated_at": stats.generated_at,
            }
            if stats
            else dict((existing_block or {}).get("stats") or {})
        ),
        "failures": failures,
    }
    title = release_title(cfg, release)
    notes = render_notes(
        release, block, header=release_notes.render_header(cfg, release, block)
    )
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
        if stats_path is not None:
            uploads.append(stats_path)
        uploaded = gh.upload(cfg.mirror_repo, tag, uploads, log=log)
        log(
            f"  published {len(ordered_results)} portable package(s) for {tag} "
            f"({uploaded['uploaded']} new, {uploaded['skipped']} unchanged)"
        )

    return {
        "tag": tag,
        "action": action,
        "title": title,
        "assets": [r.path.name for r in ordered_results],
        "original_assets": original_names,
        "stats": stats.totals if stats else dict((existing_block or {}).get("stats") or {}),
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
        previous_stats=read_published_stats(
            cfg, existing, gh, work, release_tag(release), log
        ),
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


def publish_repository_buttons(
    cfg: MirrorConfig,
    gh: GitHub,
    work: Path,
    log: Log,
    dry_run: bool,
) -> None:
    """Publish the button SVGs where raw.githubusercontent.com serves them.

    Release asset downloads are application/octet-stream on GitHub, so an
    ``<img>`` pointed at a release asset is broken.  The repository copy is
    served as image/svg+xml and is shared by every release in the mirror.
    """

    if dry_run:
        log("  dry run: release buttons would be published to the mirror branch")
        return
    if not cfg.mirror_repo:
        return
    paths = release_notes.write_button_assets(
        work / "release-buttons", release_notes.BUTTONS.keys()
    )
    changed = 0
    for path in paths:
        if gh.put_file(
            cfg.mirror_repo,
            f"{release_notes.REPOSITORY_BUTTON_DIR}/{path.name}",
            path.read_text(encoding="utf-8"),
            "Update portable release buttons",
            cfg.branch,
        ):
            changed += 1
    log(
        f"  release buttons: {len(paths)} ready, {changed} updated on "
        f"{cfg.branch}"
    )


def rebuild_index(cfg: MirrorConfig, gh: GitHub, args, log: Log) -> int:
    """Rewrite index.json from the published releases, without building.

    Sharded runs deliberately skip the index, because parallel jobs would
    overwrite each other's entries.  One cheap job rebuilds it afterwards from
    the build records GitHub already holds.
    """

    index_path = Path(args.index) if args.index else Path("index.json")
    previous: Optional[Dict[str, Any]] = None
    if index_path.is_file():
        try:
            previous = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None
    stale: Dict[str, Mapping[str, Any]] = {
        str(entry.get("tag")): entry
        for entry in (previous or {}).get("releases") or []
        if isinstance(entry, dict) and entry.get("tag")
    }
    try:
        source_by_tag = {
            release_tag(release): release
            for release in gh.list_releases(cfg.source_repo)
        }
    except (GitHubError, ApiError) as exc:
        log(f"  warning: could not read {cfg.source_repo} releases: {exc}")
        source_by_tag = {}
    updates: Dict[str, Mapping[str, Any]] = {}
    for release in gh.list_releases(cfg.mirror_repo):
        tag = release_tag(release)
        block = parse_block(release.get("body"))
        if not block or not block.get("assets"):
            log(f"  skipping {tag}: no build record")
            continue
        source = source_by_tag.get(tag)
        entry = build_index_entry(
            cfg, source or mirror_release_as_source(cfg, release), block
        )
        if source is None:
            # The upstream release is gone.  Keep the provenance and the
            # original notes the index already published for this tag.
            old = stale.get(tag) or {}
            for key in ("source_url", "source_title", "source_notes"):
                if old.get(key):
                    entry[key] = old[key]
        updates[tag] = entry
        log(f"  indexed {tag}: {len(entry.get('assets') or [])} package(s)")
    index = make_index(cfg, updates, previous, (previous or {}).get("source_readme"))
    write_index(cfg, index, index_path, log)
    if getattr(args, "publish_index", False):
        publish_index(cfg, gh, index, log)
    return 0


def sync(args, sink: Optional[Callable[[str], None]] = None) -> int:
    log = Log(args.verbose, sink)
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

    if getattr(args, "index_only", False):
        return rebuild_index(cfg, gh, args, log)

    mirror_source = bool(getattr(args, "mirror_source", False))
    updater = resolve_updater_assets(cfg, gh, log)
    post_install = resolve_post_install_for(cfg, gh, log)
    if mirror_source:
        releases = filter_source_releases(
            cfg, mirror_source_releases(cfg, gh, log), args
        )
        log(
            f"re-packing {len(releases)} release(s) from {cfg.mirror_repo or '(dry run)'} "
            f"stored firmware into {cfg.mirror_repo or '(dry run)'}"
        )
    else:
        releases = filter_source_releases(
            cfg, gh.list_releases(cfg.source_repo), args
        )
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
        "warnings": [],
        "failures": [],
    }
    index_updates: Dict[str, Optional[Mapping[str, Any]]] = {}
    readme_info: Optional[Dict[str, Any]] = None

    # Download a portable app only when a release actually needs it.  A run
    # where every release is current then costs API calls only.
    updater_paths: Dict[str, Path] = {}
    driver_paths: Dict[str, Path] = {}
    updater_lock = threading.Lock()

    def ensure_updater(key: str) -> Path:
        with updater_lock:
            if key not in updater_paths:
                info = updater[key]
                log(f"  preparing updater for {key}")
                updater_paths[key] = gh.download(
                    str(info.get("repo") or cfg.updater_repo),
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

    def ensure_driver(key: str) -> Optional[Path]:
        info = (updater.get(key) or {}).get("driver")
        if not isinstance(info, Mapping):
            return None
        with updater_lock:
            if key not in driver_paths:
                log(f"  preparing driver for {key}")
                driver_paths[key] = gh.download(
                    str(info.get("repo") or cfg.updater_repo),
                    {
                        "name": info["name"],
                        "size": info["size"],
                        "id": info.get("id"),
                        "browser_download_url": info.get("url"),
                    },
                    work_root / "updater" / str(info["tag"]) / str(info["name"]),
                    log=log,
                )
            return driver_paths[key]

    try:
        try:
            readme_info = sync_source_readme(
                cfg, gh, work_root, log, args.dry_run
            )
        except (GitHubError, ApiError, OSError) as exc:
            log(f"  warning: could not mirror source README: {exc}")
            report["failures"].append({"tag": "(readme)", "error": str(exc)})
        report["source_readme"] = readme_info
        publish_repository_buttons(cfg, gh, work_root, log, args.dry_run)

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
                # Older builds appended a visible footer.  Refresh just the
                # notes so current releases get the original body back without
                # rebuilding their packages.
                notes = render_notes(
                    release,
                    block,
                    header=release_notes.render_header(cfg, release, block),
                )
                if (
                    not args.dry_run
                    and str(existing.get("body") or "").strip() != notes.strip()
                ):
                    try:
                        notes_path = work_root / tag / "notes.md"
                        notes_path.parent.mkdir(parents=True, exist_ok=True)
                        notes_path.write_text(notes, encoding="utf-8")
                        gh.ensure_release(
                            cfg.mirror_repo,
                            tag,
                            release_title(cfg, release),
                            notes_path,
                            log=log,
                        )
                        log(f"  refreshed release notes for {tag}")
                    except (GitHubError, ApiError, OSError) as exc:
                        log(f"  warning: could not refresh release notes: {exc}")
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
                    post_install,
                    ensure_updater,
                    work_root,
                    log,
                    dry_run=args.dry_run,
                    copy_originals=not mirror_source,
                    keep_stats=mirror_source
                    or bool(getattr(args, "skip_stats", False)),
                    ensure_driver=ensure_driver,
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
                    # Some platforms may fail while the remaining platforms
                    # publish successfully.  Keep the warnings in the report
                    # but do not stop the index commit or turn the whole mirror
                    # red for one unsupported platform combination.
                    report["warnings"].append(
                        {"tag": tag, "warning": "; ".join(result["failures"])}
                    )
            elif result["action"] == "failed":
                # A release whose packages could not be built must fail the
                # run; otherwise the workflow reports success having published
                # nothing for that tag.
                report["failures"].append(
                    {"tag": tag, "error": str(result.get("reason") or "packaging failed")}
                )
            elif result["action"] == "skipped":
                # A release that can no longer be rebuilt (for example, its
                # source zip now contains two alternate scatters) may still
                # have a valid previously published package.  Keep that
                # entry rather than dropping it from the public index.
                if existing and block and block.get("assets"):
                    index_updates[tag] = build_index_entry(cfg, release, block)
                else:
                    index_updates[tag] = None

            if args.keep_work:
                log(f"  work kept at {work_root / tag}")
            else:
                shutil.rmtree(work_root / tag, ignore_errors=True)

        if not args.skip_index and not args.dry_run:
            index = make_index(cfg, index_updates, previous_index, readme_info)
            write_index(cfg, index, index_path, log)
            if getattr(args, "publish_index", False):
                publish_index(cfg, gh, index, log)
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
    if report.get("warnings"):
        lines += ["", "### Warnings", ""]
        for warning in report["warnings"]:
            lines.append(
                f"- `{warning.get('tag')}`: {warning.get('warning')}"
            )
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
    version = release_version(tag, str(release.get("name") or ""))
    updater = resolve_updater_assets(cfg, gh, log)
    post_install = resolve_post_install_for(cfg, gh, log)
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
            package = inspect_firmware(
                path,
                source_repo=cfg.source_repo,
                preferred_scatter=cfg.scatter,
            )
            if cfg.require_scatter and not package.scatters:
                log(f"skipping {asset['name']}: no scatter")
                continue
            meta = resolve_metadata(cfg, str(asset["name"]))
            ini_text = fw_package_ini(
                meta["software_name"],
                version,
                meta["target_model"],
                meta["target_type"],
                meta["description"] or str(release.get("name") or ""),
            )
            for platform in cfg.platforms:
                if platform.key not in updater_paths:
                    continue
                out_path = out_dir / output_name(str(asset["name"]), platform)
                post = post_install.render(platform.key) if post_install else None
                post_name, post_data = post if post else ("", b"")
                result = build_portable_package(
                    updater_paths[platform.key],
                    package,
                    platform,
                    out_path,
                    ini_text,
                    meta["software_name"],
                    version,
                    str(asset["name"]),
                    log=log,
                    source_repo=cfg.source_repo,
                    post_install_name=post_name,
                    post_install_data=post_data,
                    target_model=meta["target_model"],
                    target_type=meta["target_type"],
                    driver_path=updater_paths.get(platform.key + "::driver"),
                )
                manifest["outputs"].append(
                    {
                        "name": result.path.name,
                        "source_asset": result.source_asset,
                        "platform": result.platform,
                        "model": meta["target_model"],
                        "type": meta["target_type"],
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
    resolve_post_install_for(cfg, gh, log)
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
    common.add_argument(
        "--shard",
        help="build only one slice of the selected releases, written INDEX/TOTAL",
    )
    common.add_argument(
        "--mirror-source",
        action="store_true",
        help="re-pack the mirror's own stored firmware instead of source releases",
    )
    common.add_argument(
        "--index-only",
        action="store_true",
        help="rewrite index.json from published releases without building",
    )
    common.add_argument(
        "--skip-stats",
        action="store_true",
        help="keep the published stats.txt instead of regenerating it",
    )
    common.add_argument("--json-report", default="mirror-report.json")
    common.add_argument("--verbose", action="store_true")
    return common


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror firmware releases as portable flashing packages."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = _common_options()
    sync_cmd = sub.add_parser(
        "sync", parents=[common], help="build and publish every release"
    )
    sync_cmd.add_argument(
        "--publish-index",
        action="store_true",
        help="commit index.json to the mirror repository (for local runs)",
    )
    build_cmd = sub.add_parser(
        "build", parents=[common], help="build one release locally, without uploading"
    )
    build_cmd.add_argument("--tag", required=True)
    build_cmd.add_argument("--out", help="output directory (default dist/<tag>)")
    repack_cmd = sub.add_parser(
        "repack",
        parents=[common],
        help="re-package a mirror's stored firmware against the current updater",
    )
    repack_cmd.add_argument(
        "--publish-index",
        action="store_true",
        help="commit index.json to the mirror repository (for local runs)",
    )
    index_cmd = sub.add_parser(
        "index",
        parents=[common],
        help="rebuild index.json from published releases without building",
    )
    index_cmd.add_argument(
        "--publish-index",
        action="store_true",
        help="commit index.json to the mirror repository (for local runs)",
    )
    sub.add_parser("status", parents=[common], help="show which mirror releases are current")
    review_cmd = sub.add_parser(
        "review",
        parents=[common],
        help="report missing or changed assets without publishing anything",
    )
    review_cmd.add_argument("--json", action="store_true", help="print the report as JSON")
    review_cmd.set_defaults(json_report="review-report.json")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return build_only(args)
        if args.command == "status":
            return status(args)
        if args.command == "review":
            return review(args)
        if args.command == "repack":
            args.mirror_source = True
            args.skip_stats = True
            return sync(args)
        if args.command == "index":
            args.index_only = True
            return sync(args)
        return sync(args)
    except (ConfigError, GitHubError, ApiError, PackagingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
