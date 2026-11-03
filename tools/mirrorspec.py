"""Configuration, naming and release-metadata rules for the portable mirror.

This module has no network or filesystem side effects beyond loading the JSON
config.  It is the single place that defines:

* which source assets count as firmware zips
* what a portable package is called for each platform
* the ``fw_package.ini`` written into every package
* the hidden fingerprint block stored in each mirrored release body

Keeping these rules here lets ``tests/`` exercise them without GitHub access.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence

# Bump when the produced zip layout or metadata changes so existing mirror
# releases are rebuilt instead of being treated as up to date.
# 2: macOS packages are Install Firmware.app with firmware in Firmware/.
# 3: firmware zips are reduced to the scatter, its listed images and the icon.
BUILDER_VERSION = 3

DEFAULT_BRANCH = "main"
DEFAULT_TITLE_PREFIX = "Portable Flashing Package: "
DEFAULT_UPDATER_REPO = "y1-community/updater-ce-neo"
DEFAULT_README_PATH = "README.md"
DEFAULT_README_COMMIT = "Mirror source README"

# Updater CE 2.5.2 preserves userdata itself, so these sources must not ship a
# second, preserve-data scatter in the generated package.
PRESERVE_TXT_REPOS = frozenset({"schulzcode/y2linux", "schulzcode/y2reborn"})


def drops_preserve_txt(source_repo: str) -> bool:
    """Whether generated packages for ``source_repo`` drop preserve-text files."""

    return str(source_repo or "").strip().lower() in PRESERVE_TXT_REPOS

# Suffixes this project owns.  Used to keep our own output out of the source
# asset selection (which matters when a clone points at another mirror).
KNOWN_SUFFIXES = (
    "-windows.zip",
    "-windows-arm64.zip",
    "-linux.zip",
    "-linux-arm64.zip",
    "-macos.zip",
)

DEFAULT_FIRMWARE_EXCLUDES = (
    "*-windows.zip",
    "*-windows-arm64.zip",
    "*-linux.zip",
    "*-linux-arm64.zip",
    "*-macos.zip",
    "*portable*.zip",
    "*source*.zip",
    "*installer*.zip",
    "*.dmg",
    "*.apk",
    "*.tar.gz",
    "*.tgz",
    "*.tar",
    "*.sha256",
    "*.json",
    "*.txt",
    "*.sig",
    "*.img.gz",
    "*.exe",
    "*.appimage",
)

# Platform defaults follow updater-ce-neo's docs/RELEASE_FILES.md.  x86-64
# names carry no architecture tag; arm64 names carry ``-arm64``.  The arm64
# entries are optional: they start being built automatically as soon as the
# updater repository publishes the matching asset.
DEFAULT_PLATFORMS: Dict[str, Dict[str, Any]] = {
    "windows": {
        "updater_asset": "UpdaterCE-Windows-Portable.zip",
        "suffix": "-windows.zip",
        "layout": "sp_flash_tool",
        "os": "windows",
        "arch": "x86-64",
    },
    "windows-arm64": {
        "updater_asset": "UpdaterCE-Windows-Portable-arm64.zip",
        "suffix": "-windows-arm64.zip",
        "layout": "sp_flash_tool",
        "os": "windows",
        "arch": "arm64",
        "optional": True,
    },
    "linux": {
        "updater_asset": "UpdaterCE-Linux-Portable.zip",
        "suffix": "-linux.zip",
        "layout": "sp_flash_tool",
        "os": "linux",
        "arch": "x86-64",
    },
    "linux-arm64": {
        "updater_asset": "UpdaterCE-Linux-Portable-arm64.zip",
        "suffix": "-linux-arm64.zip",
        "layout": "sp_flash_tool",
        "os": "linux",
        "arch": "arm64",
        "optional": True,
    },
    "macos": {
        "updater_asset": "Innioasis.Updater.for.Mac.zip",
        "suffix": "-macos.zip",
        "layout": "beside_app",
        "os": "macos",
        "arch": "universal",
    },
}

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """Raised when mirror.config.json cannot be used as written."""


@dataclass(frozen=True)
class Platform:
    key: str
    suffix: str
    updater_asset: str
    layout: str
    os: str
    arch: str
    optional: bool = False
    pinned_tag: str = ""

    def describe(self) -> str:
        return f"{self.key} ({self.os}/{self.arch}, {self.layout})"


@dataclass(frozen=True)
class Rule:
    """Per-asset overrides for the ``fw_package.ini`` metadata."""

    match_asset: str
    software_name: str = ""
    target_model: str = ""
    target_type: str = ""
    description: str = ""

    def matches(self, asset_name: str) -> bool:
        return re.search(self.match_asset, asset_name, re.IGNORECASE) is not None


@dataclass
class MirrorConfig:
    source_repo: str
    mirror_repo: str
    branch: str
    updater_repo: str
    title_prefix: str
    copy_original_assets: bool
    include_prereleases: bool
    build_workers: int
    firmware_include: List[str]
    firmware_exclude: List[str]
    require_scatter: bool
    software_name: str
    target_model: str
    target_type: str
    description: str
    rules: List[Rule]
    platforms: List[Platform]
    readme_enabled: bool = True
    readme_path: str = DEFAULT_README_PATH
    readme_commit_message: str = DEFAULT_README_COMMIT
    tags_allow: List[str] = field(default_factory=list)
    tags_exclude: List[str] = field(default_factory=list)
    max_asset_bytes: int = 2 * 1024 * 1024 * 1024
    config_path: Optional[Path] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def platform(self, key: str) -> Platform:
        for p in self.platforms:
            if p.key == key:
                return p
        raise KeyError(key)

    def describe(self) -> str:
        return (
            f"source={self.source_repo} mirror={self.mirror_repo or '(unset)'} "
            f"branch={self.branch} platforms={','.join(p.key for p in self.platforms)} "
            f"readme={self.readme_path if self.readme_enabled else '(disabled)'}"
        )


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _as_list(value: Any, name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{name} must be a string or a list of strings")
    return list(value)


def load_config(
    path: str = "mirror.config.json",
    source_repo: Optional[str] = None,
    mirror_repo: Optional[str] = None,
    platforms: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> MirrorConfig:
    """Load and validate the mirror config, applying CLI/env overrides."""

    env = environ if environ is not None else os.environ
    config_path = Path(path)
    _expect(config_path.is_file(), f"config file not found: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc
    _expect(isinstance(raw, dict), f"{config_path} must contain a JSON object")

    src = source_repo or env.get("MIRROR_SOURCE_REPO") or raw.get("source_repo") or ""
    dst = (
        mirror_repo
        or raw.get("mirror_repo")
        or env.get("MIRROR_MIRROR_REPO")
        or env.get("GITHUB_REPOSITORY")
        or ""
    )
    _expect(bool(src), "source_repo is required (config, --source-repo or MIRROR_SOURCE_REPO)")
    _expect(bool(_REPO_RE.match(src)), f"source_repo must look like 'owner/repo', got {src!r}")
    if dst:
        _expect(bool(_REPO_RE.match(dst)), f"mirror_repo must look like 'owner/repo', got {dst!r}")

    firmware_raw = raw.get("firmware") or {}
    _expect(isinstance(firmware_raw, dict), "firmware must be an object")
    include = _as_list(firmware_raw.get("include"), "firmware.include") or ["*.zip"]
    exclude = _as_list(firmware_raw.get("exclude"), "firmware.exclude") or list(
        DEFAULT_FIRMWARE_EXCLUDES
    )

    defaults = raw.get("defaults") or {}
    _expect(isinstance(defaults, dict), "defaults must be an object")

    rules: List[Rule] = []
    for i, item in enumerate(raw.get("rules") or []):
        _expect(isinstance(item, dict), f"rules[{i}] must be an object")
        pattern = item.get("match_asset") or item.get("match")
        _expect(bool(pattern), f"rules[{i}].match_asset is required")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"rules[{i}].match_asset is not a valid regex: {exc}") from exc
        rules.append(
            Rule(
                match_asset=pattern,
                software_name=str(item.get("software_name") or ""),
                target_model=str(item.get("target_model") or ""),
                target_type=str(item.get("target_type") or ""),
                description=str(item.get("description") or ""),
            )
        )

    platform_raw = raw.get("platforms") or {}
    _expect(isinstance(platform_raw, dict), "platforms must be an object of overrides")
    selected = list(platforms) if platforms else None
    built: List[Platform] = []

    # An explicit platforms object is the complete list: keys omitted from it
    # are disabled.  Without one, all built-in platforms are enabled and the
    # optional arm64 entries are simply skipped when upstream has no asset.
    if platform_raw:
        ordered_keys = list(platform_raw.keys())
    else:
        ordered_keys = list(DEFAULT_PLATFORMS.keys())

    for key in ordered_keys:
        if selected and key not in selected:
            continue
        base = dict(DEFAULT_PLATFORMS.get(key, {}))
        override = platform_raw.get(key) or {}
        _expect(isinstance(override, dict), f"platforms.{key} must be an object")
        if override.get("enabled") is False:
            continue
        base.update(override)
        _expect(bool(base.get("updater_asset")), f"platforms.{key}.updater_asset is required")
        _expect(bool(base.get("suffix")), f"platforms.{key}.suffix is required")
        layout = base.get("layout") or "sp_flash_tool"
        _expect(
            layout in ("sp_flash_tool", "beside_app"),
            f"platforms.{key}.layout must be 'sp_flash_tool' or 'beside_app'",
        )
        built.append(
            Platform(
                key=key,
                suffix=str(base["suffix"]),
                updater_asset=str(base["updater_asset"]),
                layout=layout,
                os=str(base.get("os") or ""),
                arch=str(base.get("arch") or ""),
                optional=bool(base.get("optional")),
                pinned_tag=str(base.get("tag") or ""),
            )
        )

    if not built:
        raise ConfigError("no platforms are enabled")
    unknown = [p for p in (selected or []) if p not in [b.key for b in built]]
    if unknown:
        raise ConfigError(
            "unknown or disabled platform(s): " + ", ".join(unknown)
        )

    releases_raw = raw.get("releases") or {}
    _expect(isinstance(releases_raw, dict), "releases must be an object")

    readme_raw = raw.get("source_readme") or {}
    _expect(isinstance(readme_raw, dict), "source_readme must be an object")
    readme_enabled = readme_raw.get("enabled", True)
    _expect(isinstance(readme_enabled, bool), "source_readme.enabled must be true or false")
    readme_path = str(readme_raw.get("path") or DEFAULT_README_PATH).strip()
    pure_path = PurePosixPath(readme_path)
    _expect(
        bool(readme_path)
        and not pure_path.is_absolute()
        and ".." not in pure_path.parts
        and "\\" not in readme_path
        and readme_path not in (".", ".."),
        "source_readme.path must be a relative repository path without '..'",
    )
    readme_commit_message = str(
        readme_raw.get("commit_message") or DEFAULT_README_COMMIT
    ).strip()
    _expect(bool(readme_commit_message), "source_readme.commit_message must not be empty")

    workers = raw.get("build_workers", 2)
    _expect(isinstance(workers, int) and 1 <= workers <= 8, "build_workers must be 1..8")

    max_bytes = raw.get("max_asset_bytes", 2 * 1024 * 1024 * 1024)
    _expect(
        isinstance(max_bytes, int) and max_bytes > 0,
        "max_asset_bytes must be a positive integer",
    )

    cfg = MirrorConfig(
        source_repo=src,
        mirror_repo=dst,
        branch=str(raw.get("branch") or DEFAULT_BRANCH),
        updater_repo=str(raw.get("updater_repo") or DEFAULT_UPDATER_REPO),
        title_prefix=str(raw.get("title_prefix") or DEFAULT_TITLE_PREFIX),
        copy_original_assets=bool(raw.get("copy_original_assets", True)),
        include_prereleases=bool(releases_raw.get("include_prereleases", True)),
        build_workers=workers,
        firmware_include=include,
        firmware_exclude=exclude,
        require_scatter=bool(firmware_raw.get("require_scatter", True)),
        software_name=str(defaults.get("software_name") or ""),
        target_model=str(defaults.get("target_model") or ""),
        target_type=str(defaults.get("target_type") or ""),
        description=str(defaults.get("description") or ""),
        rules=rules,
        platforms=built,
        readme_enabled=readme_enabled,
        readme_path=readme_path,
        readme_commit_message=readme_commit_message,
        tags_allow=_as_list(releases_raw.get("tags"), "releases.tags"),
        tags_exclude=_as_list(releases_raw.get("exclude_tags"), "releases.exclude_tags"),
        max_asset_bytes=max_bytes,
        config_path=config_path,
        raw=raw,
    )
    _expect(
        bool(cfg.platforms),
        "no platforms enabled after applying overrides",
    )
    return cfg


def asset_matches(pattern: str, name: str) -> bool:
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def firmware_asset_name_matches(name: str, cfg: MirrorConfig) -> bool:
    """True when an asset name is a firmware zip, independent of its size."""

    if not name or not name.lower().endswith(".zip"):
        return False
    if any(asset_matches(p, name) for p in cfg.firmware_exclude):
        return False
    if any(name.lower().endswith(s.lower()) for s in KNOWN_SUFFIXES):
        return False
    return any(asset_matches(p, name) for p in cfg.firmware_include)


def select_firmware_assets(
    assets: Sequence[Mapping[str, Any]], cfg: MirrorConfig
) -> List[Mapping[str, Any]]:
    """Return source assets that look like flashable firmware zips."""

    selected: List[Mapping[str, Any]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        if not firmware_asset_name_matches(name, cfg):
            continue
        if int(asset.get("size") or 0) > cfg.max_asset_bytes:
            continue
        selected.append(asset)
    return selected


def asset_base_name(name: str) -> str:
    return name[:-4] if name.lower().endswith(".zip") else name


def output_name(asset_name: str, platform: Platform) -> str:
    return asset_base_name(asset_name) + platform.suffix


def resolve_metadata(cfg: MirrorConfig, asset_name: str) -> Dict[str, str]:
    """Apply the first matching rule for this asset, then config defaults."""

    meta = {
        "software_name": cfg.software_name,
        "target_model": cfg.target_model,
        "target_type": cfg.target_type,
        "description": cfg.description,
    }
    for rule in cfg.rules:
        if not rule.matches(asset_name):
            continue
        for key in ("software_name", "target_model", "target_type", "description"):
            value = getattr(rule, key)
            if value:
                meta[key] = value
        break
    return meta


def fw_package_ini(
    software_name: str,
    version: str,
    target_model: str,
    target_type: str,
    description: str = "",
    enabled: bool = True,
) -> str:
    """Render the ``fw_package.ini`` that Updater CE reads next to a scatter."""

    lines = ["[Firmware]"]

    def add(key: str, value: str) -> None:
        clean = " ".join(str(value).splitlines()).strip()
        if clean:
            lines.append(f"{key} = {clean}")

    add("software_name", software_name)
    add("version", version)
    add("target_model", target_model)
    add("target_type", target_type)
    add("description", description)
    lines.append(f"enabled = {'true' if enabled else 'false'}")
    return "\r\n".join(lines) + "\r\n"


def release_version(tag: str) -> str:
    """The version written into fw_package.ini.  Tags are used as published."""

    tag = str(tag).strip()
    return tag[1:] if len(tag) > 1 and tag[0] in ("v", "V") else tag


def release_title(cfg: MirrorConfig, release: Mapping[str, Any]) -> str:
    tag = str(release.get("tag_name") or release.get("tag") or "")
    base = str(release.get("name") or "").strip() or tag
    if cfg.title_prefix and not base.lower().startswith(cfg.title_prefix.lower()):
        return cfg.title_prefix + base
    return base


def config_fingerprint(cfg: MirrorConfig) -> str:
    payload = {
        "v": BUILDER_VERSION,
        "platforms": [
            {
                "key": p.key,
                "suffix": p.suffix,
                "asset": p.updater_asset,
                "layout": p.layout,
                "pinned": p.pinned_tag,
            }
            for p in cfg.platforms
        ],
        "firmware": {
            "include": cfg.firmware_include,
            "exclude": cfg.firmware_exclude,
            "require_scatter": cfg.require_scatter,
        },
        "copy_original_assets": cfg.copy_original_assets,
        "defaults": {
            "software_name": cfg.software_name,
            "target_model": cfg.target_model,
            "target_type": cfg.target_type,
            "description": cfg.description,
        },
        "rules": [
            {
                "match": r.match_asset,
                "software_name": r.software_name,
                "target_model": r.target_model,
                "target_type": r.target_type,
                "description": r.description,
            }
            for r in cfg.rules
        ],
    }
    if drops_preserve_txt(cfg.source_repo):
        # Only these configs need a new fingerprint; other mirrors keep theirs.
        payload["drop_preserve_txt"] = True
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def release_fingerprint(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    updater_assets: Mapping[str, Mapping[str, Any]],
) -> str:
    """Identify the exact inputs behind a built mirror release."""

    source_assets = sorted(
        (
            {
                "name": str(a.get("name") or ""),
                "size": int(a.get("size") or 0),
                "id": a.get("id"),
            }
            for a in release.get("assets", [])
        ),
        key=lambda a: a["name"],
    )
    payload = {
        "v": BUILDER_VERSION,
        "config": config_fingerprint(cfg),
        "source": {
            "repo": cfg.source_repo,
            "tag": str(release.get("tag_name") or ""),
            "assets": source_assets,
        },
        "updater": {
            key: {
                "tag": value.get("tag"),
                "name": value.get("name"),
                "size": value.get("size"),
                "id": value.get("id"),
            }
            for key, value in sorted(updater_assets.items())
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_BLOCK_RE = re.compile(r"<!--\s*portable-mirror:(\{.*?\})\s*-->", re.DOTALL)


def parse_block(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Read the hidden build record from a release body, if present."""

    if not body:
        return None
    match = _BLOCK_RE.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def render_notes(release: Mapping[str, Any], block: Mapping[str, Any]) -> str:
    """Original release notes followed by the invisible build record."""

    body = str(release.get("body") or "").strip()
    marker = (
        "<!-- portable-mirror:"
        f"{json.dumps(block, sort_keys=True, separators=(',', ':'))} -->"
    )
    return (body + "\n\n" + marker).strip() + "\n"


def macos_readme(app_name: str, firmware_asset: str, software: str, version: str) -> str:
    title = " ".join(part for part in (software, version) if part)
    lines = [
        "Innioasis Updater CE + Firmware Bundle",
        "=====================================",
        "",
        f"Firmware: {title or software or 'Innioasis firmware'} ({firmware_asset})",
        "",
        "Quick start on macOS:",
        f'1. Keep "{app_name}" and the "Firmware" folder together; you can move',
        "   the whole folder to Applications.",
        "2. Launch Install Firmware and follow the on-screen prompt.",
        "3. If macOS prevents opening the app, open System Settings > Privacy & Security,",
        '   scroll to Security and click "Open Anyway", then confirm.',
        "",
        "The firmware files and fw_package.ini live in the Firmware folder next to",
        "the app. If the app asks for a firmware folder, choose that one.",
        "",
    ]
    return "\n".join(lines)
