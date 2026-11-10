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
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence

# Bump when the produced zip layout or metadata changes so existing mirror
# releases are rebuilt instead of being treated as up to date.
# 2: macOS packages are Install Firmware.app with firmware in Firmware/.
# 3: firmware zips are reduced to the scatter, its listed images and the icon.
# 4: fw_package.ini resolves the user-facing version from the release title.
# 5: macOS uses the signed DMG beside a lowercase firmware/ folder, and every
#    package includes the product's post-install instructions when present.
# 6: the macOS DMG is renamed for users, its README explains Install from file,
#    and release bodies start with generated device/platform download buttons.
# 7: button SVGs are served from the mirror branch so GitHub renders them
#    instead of returning the release-asset application/octet-stream download.
# 8: SP Flash Tool history names the packaged scatter and DA with bare names,
#    including a firmware-provided DA when one exists.
# 9: macOS metadata trees (__MACOSX/_MACOSX) are stripped from every package.
# 10: every package carries only the one scatter declared by its source zip;
#     preserve-data and alternate scatters are never copied.
# 11: reference scatters shipped inside the portable app itself
#     (_internal/assets/compat) are stripped, so the firmware's scatter is the
#     only scatter anywhere in a Windows/Linux package.
# 12: every zip member is deflated at level 9, including streamed copies and
#     the macOS DMG; original firmware zips are repacked the same way and keep
#     only standard *_Android_scatter.txt layouts when several are present.
# 13: Windows XP/7/8 packages are built from the flash-tool-only asset and
#     carry donate.txt, for computers that cannot run Updater CE.
BUILDER_VERSION = 13

DEFAULT_BRANCH = "main"
DEFAULT_TITLE_PREFIX = "Portable Flashing Package: "
DEFAULT_UPDATER_REPO = "y1-community/updater-ce-neo"
DEFAULT_README_PATH = "README.md"
DEFAULT_README_COMMIT = "Mirror source README"

# Suffixes this project owns.  Used to keep our own output out of the source
# asset selection (which matters when a clone points at another mirror).
KNOWN_SUFFIXES = (
    "-windows-legacy.zip",
    "-windows.zip",
    "-windows-arm64.zip",
    "-linux.zip",
    "-linux-arm64.zip",
    "-macos.zip",
)

# Legacy Windows packages wrap the SP Flash Tool instead of Updater CE.  They
# must keep working on Windows XP, Vista, 7 and 8/8.1, where the PySide6 app
# cannot run, so they carry a plain-text note with the ways to support the
# project rather than a link to the app's own pages.
DONATION_URL = "https://innioasis.app/support_devs.html"
DONATION_LINKS = (
    ("Buy Me a Coffee", "https://buymeacoffee.com/ryanspecter"),
    ("Boosty", "https://boosty.to/ryanspecter"),
    ("All support options", DONATION_URL),
)

DEFAULT_FIRMWARE_EXCLUDES = (
    "*-windows-legacy.zip",
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
    # Windows XP/Vista/7/8/8.1 and 32-bit Windows cannot run Updater CE.  The
    # updater repository publishes the SP Flash Tool on its own, so the same
    # firmware is packed beside that tool instead of the app.  Optional until
    # the first flash-tool-only asset is published upstream.
    "windows-legacy": {
        "updater_asset": "Innioasis-SP-Flash-Tool-windows-legacy.zip",
        # Older runs of the updater build published the same tool under a
        # plainer name.  Either asset is accepted so a mirror never fails just
        # because the upstream name changed.
        "updater_asset_alternates": ["SP_Flash_Tool-Windows.zip"],
        "suffix": "-windows-legacy.zip",
        "layout": "sp_flash_tool",
        "os": "windows",
        "arch": "x86",
        "optional": True,
        "legacy": True,
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
        "updater_asset": "Innioasis.Updater.for.Mac.dmg",
        "suffix": "-macos.zip",
        "layout": "beside_dmg",
        "os": "macos",
        "arch": "universal",
        "updater_repo": "y1-community/innioasis-updater",
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
    updater_repo: str = ""
    # Legacy packages wrap the flash tool alone and explain how to support the
    # project, because the Updater CE app cannot run on that computer.
    legacy: bool = False
    # Other published names for the same upstream asset.
    alt_assets: Tuple[str, ...] = ()

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
    post_installs: List[str] = field(default_factory=list)
    readme_enabled: bool = True
    readme_path: str = DEFAULT_README_PATH
    readme_commit_message: str = DEFAULT_README_COMMIT
    tags_allow: List[str] = field(default_factory=list)
    tags_exclude: List[str] = field(default_factory=list)
    # Basename of the scatter to use when a stock archive carries more than
    # one hardware layout (for example MT6572 and MT6582 in rom_y2.zip).
    scatter: str = ""
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
        alternates = _as_list(
            base.get("updater_asset_alternates"),
            f"platforms.{key}.updater_asset_alternates",
        )
        _expect(bool(base.get("suffix")), f"platforms.{key}.suffix is required")
        layout = base.get("layout") or "sp_flash_tool"
        _expect(
            layout in ("sp_flash_tool", "beside_app", "beside_dmg"),
            f"platforms.{key}.layout must be 'sp_flash_tool', 'beside_app' or 'beside_dmg'",
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
                updater_repo=str(base.get("updater_repo") or ""),
                legacy=bool(base.get("legacy")),
                alt_assets=tuple(a for a in alternates if a != base["updater_asset"]),
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
        scatter=str(firmware_raw.get("scatter") or "").strip(),
        software_name=str(defaults.get("software_name") or ""),
        target_model=str(defaults.get("target_model") or ""),
        target_type=str(defaults.get("target_type") or ""),
        description=str(defaults.get("description") or ""),
        rules=rules,
        platforms=built,
        post_installs=_as_list(
            raw.get("post_install", raw.get("post_installs")), "post_install"
        ),
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


def _release_datestamp_label(raw: str) -> Optional[str]:
    """Human date for a YYYYMMDD, YYMMDD or separated datestamp tag."""

    text = str(raw or "")
    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})[-_./]?(0[1-9]|1[0-2])[-_./]?(0[1-9]|[12]\d|3[01])(?!\d)",
        text,
    )
    year = month = day = None
    if match:
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
    else:
        short = re.search(
            r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)",
            text,
        )
        if not short:
            return None
        yy = int(short.group(1))
        year = 2000 + yy if yy < 70 else 1900 + yy
        month, day = int(short.group(2)), int(short.group(3))
    try:
        dt = datetime(year, month, day)
    except ValueError:
        return None
    return f"{dt.strftime('%B')} {dt.day} {dt.year}"


def _tag_semver(tag: str) -> Optional[str]:
    """Read the first semantic version embedded in a release tag."""

    text = str(tag or "").strip()
    if not text:
        return None
    compound = re.search(
        r"(?:^|[^\d.])(?:v|v\.)?(\d+\.\d+(?:\.\d+)?)[-_](?:[a-zA-Z0-9]+[-_]|os[-_])?v?(\d+\.\d+(?:\.\d+)?)(?:[^\d.]|$)",
        text,
        re.IGNORECASE,
    )
    if compound:
        return compound.group(1)
    match = re.search(
        r"(?:^|[^\d.])(?:v|v\.)?(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z0-9]+)?)(?:[^\d.]|$)",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _detect_title_start_or_end_semver(title: str) -> Optional[str]:
    """Detect a semantic version at the start or end of a release title."""

    text = str(title or "").strip()
    if not text:
        return None

    start = re.match(
        r"^(?:release\s+|version\s+)?(?:\[|\()?(?:v|v\.)?(\d+(?:\.\d+)+)(?:\]|\))?(?:$|[\s:\-_|,])",
        text,
        re.IGNORECASE,
    )
    if start:
        return start.group(1)

    # Strip trailing designations such as (Beta), (240p) or [Preview].
    candidate = text
    for _ in range(4):
        cleaned = re.sub(
            r"[\s\-_]*[\(\[](?:beta\d*|alpha\d*|rc\d*|preview|240p|360p|adb|wifi|wi-fi|root|magisk)[\)\]]\s*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        if cleaned == candidate:
            break
        candidate = cleaned.strip()

    end = re.search(
        r"(?:^|[\s:\-_|,\(\[])(?:v|v\.)?(\d+(?:\.\d+)+)(?:\]|\))?\s*$",
        candidate,
        re.IGNORECASE,
    )
    return end.group(1) if end else None


def _semver_from_release_title(title: str) -> Optional[str]:
    """Read a title version when its tag has no usable version."""

    text = str(title or "").strip()
    if not text:
        return None
    detected = _detect_title_start_or_end_semver(text)
    if detected:
        return detected
    after_prerelease = re.search(
        r"\b(?:beta|alpha|rc)\s*v?(\d+(?:\.\d+)+)\b", text, re.IGNORECASE
    )
    if after_prerelease:
        return after_prerelease.group(1)
    versions = re.findall(r"(?<![\d.])(\d+\.\d+(?:\.\d+)+)(?![\d.])", text)
    return versions[0] if len(versions) == 1 else None


def release_version(tag: str, title: str = "") -> str:
    """Version displayed by fw_package.ini, matching Updater CE.

    A semantic version at the start or end of the original release title wins
    over a build tag.  Otherwise use a semantic version embedded in the tag,
    then a datestamp in the tag formatted as a readable date, then the tag as
    published.  This is the same precedence Updater CE uses when it labels a
    firmware release.
    """

    clean_tag = str(tag or "").strip()
    clean_title = str(title or "").strip()
    title_version = _detect_title_start_or_end_semver(clean_title)
    if title_version:
        return title_version
    tag_version = _tag_semver(clean_tag)
    if tag_version:
        return tag_version
    date_version = _release_datestamp_label(clean_tag)
    if date_version:
        return date_version
    fallback = _semver_from_release_title(clean_title)
    if fallback:
        return fallback
    return clean_tag[1:] if len(clean_tag) > 1 and clean_tag[0] in ("v", "V") else clean_tag


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
                "updater_repo": p.updater_repo,
                "legacy": p.legacy,
                "alt_assets": list(p.alt_assets),
            }
            for p in cfg.platforms
        ],
        "post_installs": cfg.post_installs,
        "firmware": {
            "include": cfg.firmware_include,
            "exclude": cfg.firmware_exclude,
            "require_scatter": cfg.require_scatter,
            # Only include an explicit choice.  Omitting it keeps the
            # fingerprint of every existing mirror unchanged.
            **({"scatter": cfg.scatter} if cfg.scatter else {}),
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
                "digest": str(a.get("digest") or ""),
                "updated_at": str(a.get("updated_at") or ""),
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
            "name": str(release.get("name") or ""),
            "assets": source_assets,
        },
        "updater": {
            key: {
                "tag": value.get("tag"),
                "name": value.get("name"),
                "size": value.get("size"),
                "id": value.get("id"),
                "digest": value.get("digest"),
                "updated_at": value.get("updated_at"),
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


def strip_block(body: Optional[str]) -> str:
    """Return a release body without its hidden build record."""

    return _BLOCK_RE.sub("", str(body or "")).strip()


def render_notes(
    release: Mapping[str, Any], block: Mapping[str, Any], header: str = ""
) -> str:
    """Generated buttons, original release notes and the invisible build record."""

    body = str(release.get("body") or "").strip()
    marker = (
        "<!-- portable-mirror:"
        f"{json.dumps(block, sort_keys=True, separators=(',', ':'))} -->"
    )
    sections = []
    if header.strip():
        sections.append(header.strip())
    if body:
        sections.append(body)
    sections.append(marker)
    return "\n\n".join(sections).strip() + "\n"


def legacy_donate_txt(software: str, version: str, firmware_asset: str) -> str:
    """A plain-text note for computers that cannot run Updater CE.

    It is written by every legacy package so a Windows XP/7/8 user still sees
    where the project can be supported after the install, without opening a
    browser first.  Every URL is spelled out so it also works when copied to a
    machine that is not online.
    """

    title = " ".join(part for part in (software, version) if part).strip()
    lines = [
        "Thank you for using this Innioasis firmware package.",
        "",
        f"Firmware: {title or software or 'Innioasis firmware'}",
        f"Download: {firmware_asset}",
        "",
        "Updater CE cannot run on this computer, so this download includes the",
        "same SP Flash Tool that Updater CE bundles, with the firmware already",
        "in its SP_Flash_Tool folder.",
        "",
        "If this work saved you time, you can support the community projects that",
        "keep these players usable:",
        "",
    ]
    for label, url in DONATION_LINKS:
        lines.append(f"  {label}: {url}")
    lines += [
        "",
        "Support is optional. Nothing in this package is limited by whether you",
        "donate.",
        "",
    ]
    return "\r\n".join(lines)


def legacy_readme(
    software: str, version: str, firmware_asset: str, scatter: str
) -> str:
    """Quick-start for the SP Flash Tool package on older Windows."""

    title = " ".join(part for part in (software, version) if part).strip()
    lines = [
        "Innioasis Firmware + SP Flash Tool (Windows XP/7/8, 32-bit and 64-bit)",
        "=====================================================================",
        "",
        f"Firmware: {title or software or 'Innioasis firmware'} ({firmware_asset})",
        "",
        "This download is for computers that cannot run Innioasis Updater CE:",
        "Windows XP, Vista, 7, 8 or 8.1, and 32-bit Windows.",
        "",
        "How to install the firmware:",
        "1. Right-click this zip and choose Extract All. Do not run anything",
        "   from inside the zip window.",
        "2. Open the SP_Flash_Tool folder and run flash_tool.exe.",
        "3. In the Download tab, choose the scatter file that came with this",
        f"   package ({scatter}).",
        "4. Leave every partition ticked as the scatter set it, then click",
        "   Download. When the tool is waiting, power the player fully off and",
        "   connect it with a USB cable that carries data.",
        "5. Wait for the green check mark before unplugging anything.",
        "",
        "Windows 10 and 11 can use Updater CE instead, which also covers macOS",
        "and Linux: https://innioasis.app/install/legacy-windows.html",
        "",
        "If the player is not detected, install the MediaTek USB driver in the",
        "Driver folder, restart the PC, and try another USB port.",
        "",
        "donate.txt lists the ways to support the projects behind this firmware.",
        "",
    ]
    return "\r\n".join(lines)


def macos_dmg_readme(
    dmg_name: str, firmware_folder: str, firmware_asset: str, software: str, version: str
) -> str:
    """README for the signed-DMG layout used by current macOS updaters."""

    title = " ".join(part for part in (software, version) if part)
    lines = [
        "Innioasis Updater CE + Firmware Bundle",
        "=====================================",
        "",
        f"Firmware: {title or software or 'Innioasis firmware'} ({firmware_asset})",
        "",
        "Quick start on macOS:",
        f'1. Open "{dmg_name}" and drag "Innioasis Updater" to Applications.',
        f'2. Extract this whole download and keep the "{firmware_folder}" folder',
        "   next to the DMG. It contains the firmware images and fw_package.ini.",
        "3. Launch Innioasis Updater, choose \"Install from file...\", select the",
        f'   "{firmware_folder}" folder from this download (the original software',
        "   download's Firmware folder), then click \"Install\".",
        "4. If macOS prevents opening the app, open System Settings > Privacy & Security,",
        '   scroll to Security and click "Open Anyway", then confirm.',
        "5. If this package includes an \"After Install Steps\" file, open it",
        "   after the install finishes.",
        "",
        "The firmware folder is the original software download's payload,",
        "repackaged beside the signed upstream DMG. The DMG is copied unchanged.",
        "",
    ]
    return "\n".join(lines)


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
        "2. Launch Install Firmware, choose \"Install from file...\", select the",
        "   \"Firmware\" folder, then click \"Install\".",
        "3. If macOS prevents opening the app, open System Settings > Privacy & Security,",
        '   scroll to Security and click "Open Anyway", then confirm.',
        "",
        "The firmware files and fw_package.ini live in the Firmware folder next to",
        "the app. If the app asks for a firmware folder, choose that one.",
        "",
    ]
    return "\n".join(lines)
