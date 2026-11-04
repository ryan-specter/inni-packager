#!/usr/bin/env python3
"""One-off repair for macOS assets on releases no longer covered by sync.

This is intentionally narrower than portable_mirror.sync: it rebuilds only
stale ``*-macos.zip`` assets from the original firmware zip, uploads the
replacement, and leaves the release body, block, stats and other assets alone.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import postinstall  # noqa: E402
from gh import ApiError, GitHub, GitHubError  # noqa: E402
from manifest import post_installs_for_source  # noqa: E402
from mirrorspec import (  # noqa: E402
    BUILDER_VERSION,
    ConfigError,
    MirrorConfig,
    fw_package_ini,
    load_config,
    parse_block,
    release_version,
    resolve_metadata,
)
from packager import (  # noqa: E402
    PackagingError,
    build_portable_package,
    inspect_firmware,
)
from portable_mirror import release_tag, resolve_updater_assets  # noqa: E402

REPOS = (
    "y2linux-mirror",
    "inniclassic-y2-mirror",
    "rockbox-mirror",
    "solar-mirror",
)
CUTOFF = "2026-11-03T17:22:00Z"
ORG = "y1-community"


def log(message: str) -> None:
    print(message, flush=True)


def fetch_config(gh: GitHub, repo: str, work: Path) -> MirrorConfig:
    content = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/{ORG}/{repo}/contents/mirror.config.json",
            "--jq",
            ".content",
        ],
        text=True,
    )
    text = base64.b64decode(content).decode("utf-8")
    path = work / "configs" / f"{repo}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return load_config(str(path), mirror_repo=f"{ORG}/{repo}")


def trailing_block(body: Optional[str]) -> Optional[Dict[str, Any]]:
    return parse_block(body) if body else None


def source_name_for(
    asset_name: str, block: Mapping[str, Any]
) -> str:
    for item in block.get("assets") or []:
        if str(item.get("name") or "") == asset_name:
            source = str(item.get("source_asset") or "").strip()
            if source:
                return source
    if asset_name.endswith("-macos.zip"):
        return asset_name[: -len("-macos.zip")] + ".zip"
    return asset_name


def verify_mac_layout(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    roots = {name.split("/", 1)[0] for name in names if name}
    if "Install Firmware.app" not in roots:
        raise PackagingError(f"{path.name}: Install Firmware.app is missing")
    if "Firmware" not in roots:
        raise PackagingError(f"{path.name}: Firmware/ is missing")
    if "README.txt" not in names:
        raise PackagingError(f"{path.name}: README.txt is missing")
    if "Firmware/fw_package.ini" not in names:
        raise PackagingError(f"{path.name}: Firmware/fw_package.ini is missing")
    old_apps = {root for root in roots if root.endswith(".app") and root != "Install Firmware.app"}
    if old_apps:
        raise PackagingError(f"{path.name}: old app bundle(s) present: {sorted(old_apps)}")


def download_asset(
    gh: GitHub, repo: str, asset: Mapping[str, Any], dest: Path
) -> Path:
    return gh.download(repo, asset, dest, log=log)


def repair_release(
    gh: GitHub,
    cfg: MirrorConfig,
    mirror_release: Mapping[str, Any],
    source_release: Mapping[str, Any],
    updater_path: Path,
    work_root: Path,
) -> int:
    tag = release_tag(mirror_release)
    block = trailing_block(mirror_release.get("body"))
    if not block:
        log(f"  {tag}: no build block; skipping (cannot map source assets)")
        return 0
    mac_assets = [
        asset
        for asset in mirror_release.get("assets") or []
        if str(asset.get("name") or "").endswith("-macos.zip")
        and "Source" not in str(asset.get("name") or "")
        and str(asset.get("updated_at") or "") < CUTOFF
    ]
    if not mac_assets:
        return 0

    source_assets = {
        str(asset.get("name") or ""): asset
        for asset in source_release.get("assets") or []
    }
    mirror_assets = {
        str(asset.get("name") or ""): asset
        for asset in mirror_release.get("assets") or []
    }
    platform = cfg.platform("macos")
    work = work_root / tag
    work.mkdir(parents=True, exist_ok=True)
    repaired = 0
    try:
        for mac_asset in mac_assets:
            mac_name = str(mac_asset["name"])
            source_name = source_name_for(mac_name, block)
            source_asset = source_assets.get(source_name) or mirror_assets.get(source_name)
            if not source_asset:
                log(f"  {tag}: missing original firmware {source_name}; cannot rebuild {mac_name}")
                continue

            source_repo = cfg.source_repo if source_name in source_assets else cfg.mirror_repo
            source_path = download_asset(
                gh,
                source_repo,
                source_asset,
                work / "src" / source_name,
            )
            try:
                package = inspect_firmware(source_path, source_repo=cfg.source_repo)
            except PackagingError as exc:
                log(f"  {tag}: {source_name}: not usable firmware: {exc}")
                continue
            if cfg.require_scatter and not package.scatters:
                log(f"  {tag}: {source_name}: no scatter; skipped")
                continue

            meta = resolve_metadata(cfg, source_name)
            version = release_version(tag, str(source_release.get("name") or ""))
            ini_text = fw_package_ini(
                software_name=meta["software_name"],
                version=version,
                target_model=meta["target_model"],
                target_type=meta["target_type"],
                description=meta["description"] or str(source_release.get("name") or ""),
            )
            out_path = work / mac_name
            post = post.render(platform.key) if post else None
            post_name, post_data = post if post else ("", b"")
            result = build_portable_package(
                updater_path,
                package,
                platform,
                out_path,
                ini_text,
                meta["software_name"],
                version,
                source_name,
                log=log,
                source_repo=cfg.source_repo,
                post_install_name=post_name,
                post_install_data=post_data,
            )
            verify_mac_layout(result.path)
            uploaded = gh.upload(
                cfg.mirror_repo,
                tag,
                [result.path],
                log=log,
            )
            repaired += 1
            log(
                f"  {tag}: {mac_name} -> {result.size / 1024 / 1024:.0f} MiB "
                f"({uploaded['uploaded']} uploaded, {uploaded['skipped']} unchanged)"
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return repaired


def main(argv: List[str]) -> int:
    selected = argv[1:] or list(REPOS)
    unknown = [repo for repo in selected if repo not in REPOS]
    if unknown:
        raise SystemExit(f"unknown mirror(s): {', '.join(unknown)}")

    gh = GitHub()
    root = Path(tempfile.mkdtemp(prefix="repair-macos-"))
    log(f"work: {root}")
    total = 0
    failures = 0
    try:
        for repo in selected:
            log(f"=== {repo} ===")
            try:
                cfg = fetch_config(gh, repo, root)
            except (ConfigError, GitHubError, ApiError, OSError, subprocess.CalledProcessError) as exc:
                log(f"  config failed: {exc}")
                failures += 1
                continue
            if not cfg.mirror_repo:
                cfg.mirror_repo = f"{ORG}/{repo}"

            updater = resolve_updater_assets(cfg, gh, log)
            post_urls = list(cfg.post_installs)
            if not post_urls:
                try:
                    post_urls = post_installs_for_source(cfg.source_repo)
                except (OSError, ValueError) as exc:
                    log(f"  warning: could not read post_install from manifest: {exc}")
            post = postinstall.resolve(post_urls, gh, log=log)
            mac_info = updater.get("macos")
            if not mac_info:
                log("  no macOS updater; skipped")
                failures += 1
                continue
            updater_path = gh.download(
                str(mac_info.get("repo") or cfg.updater_repo),
                {
                    "name": mac_info["name"],
                    "size": mac_info["size"],
                    "id": mac_info.get("id"),
                    "browser_download_url": mac_info.get("url"),
                },
                root / "updater" / str(mac_info["tag"]) / str(mac_info["name"]),
                log=log,
            )

            source_releases = {
                release_tag(release): release
                for release in gh.list_releases(cfg.source_repo)
            }
            for mirror_release in gh.list_releases(cfg.mirror_repo):
                tag = release_tag(mirror_release)
                block = trailing_block(mirror_release.get("body"))
                if not block or int(block.get("v") or 0) >= BUILDER_VERSION:
                    continue
                if not any(
                    str(asset.get("name") or "").endswith("-macos.zip")
                    and "Source" not in str(asset.get("name") or "")
                    and str(asset.get("updated_at") or "") < CUTOFF
                    for asset in mirror_release.get("assets") or []
                ):
                    continue
                source = source_releases.get(tag)
                if not source:
                    log(f"  {tag}: source release not found; skipped")
                    failures += 1
                    continue
                try:
                    count = repair_release(
                        gh, cfg, mirror_release, source, updater_path, root / repo
                    )
                    total += count
                except (GitHubError, ApiError, PackagingError, OSError, ValueError) as exc:
                    log(f"  {tag}: FAILED: {exc}")
                    traceback.print_exc()
                    failures += 1
            log(f"  repaired {total} macOS asset(s) so far")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    log(f"done: {total} macOS asset(s) repaired; {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
