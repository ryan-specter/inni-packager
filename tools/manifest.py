"""Read the Innioasis app-market manifest and derive mirror products.

The manifest contains one ``<package>`` per catalog row.  Several rows can
point at the same source repository (a Y1 and a Y2 release, for example), so
onboarding groups them into one product and one mirror repository.

Only ``type="img"`` rows are considered.  APK rows, comments and rows without a
valid ``owner/repo`` are ignored.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

import repos as repo_fallback

MANIFEST_URL = "https://innioasis.app/slidia_manifest.xml"
MANIFEST_URLS = tuple(repo_fallback.manifest_urls())
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")
_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_PACKAGE_TAG_RE = re.compile(r"<package\b[^>]*?/\s*>", re.DOTALL)
_ATTRIBUTE_RE = re.compile(r'([A-Za-z_][\w:.-]*)\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class ManifestPackage:
    name: str
    repo: str
    device: str
    package_type: str
    post_install: str = ""
    derivative: str = ""


@dataclass(frozen=True)
class Product:
    source_repo: str
    mirror_repo: str
    packages: Tuple[ManifestPackage, ...]

    @property
    def names(self) -> List[str]:
        seen: List[str] = []
        for package in self.packages:
            if package.name and package.name not in seen:
                seen.append(package.name)
        return seen

    @property
    def devices(self) -> List[str]:
        seen: List[str] = []
        for package in self.packages:
            device = package.device.strip().upper()
            if device and device not in seen:
                seen.append(device)
        return seen

    @property
    def post_installs(self) -> List[str]:
        seen: List[str] = []
        for package in self.packages:
            if package.post_install and package.post_install not in seen:
                seen.append(package.post_install)
        return seen

    @property
    def software_name(self) -> str:
        names = self.names
        if len(names) == 1:
            return names[0]
        return self.source_repo.split("/", 1)[-1].replace("-", " ").title()


def fetch_manifest(location: Optional[str] = None, timeout: int = 45) -> str:
    """Read the manifest from GitHub first, then the site, then a local path.

    GitHub is the primary copy so a healthy site outage does not stop the
    packager.  The repository is tried under both owner spellings, followed by
    ``innioasis.app``, exactly matching the browser's order.
    """

    if location is None or location == MANIFEST_URL:
        locations: Sequence[str] = MANIFEST_URLS
    else:
        locations = (location,)
    last_error: Optional[Exception] = None
    for candidate in locations:
        if candidate.startswith(("https://", "http://")):
            request = urllib.request.Request(
                candidate, headers={"User-Agent": "inni-packager/1.0"}
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read().decode("utf-8")
            except (OSError, ValueError) as exc:
                last_error = exc
                continue
        try:
            return Path(candidate).read_text(encoding="utf-8")
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("no manifest location configured")


def _package_from_attributes(attributes: Mapping[str, str]) -> Optional[ManifestPackage]:
    """Build one img row from XML attributes, or None when it is not usable."""

    if (attributes.get("type") or "").lower() != "img":
        return None
    repo = (attributes.get("repo") or "").strip()
    if not REPO_RE.match(repo):
        return None
    return ManifestPackage(
        name=(attributes.get("name") or "").strip(),
        repo=repo,
        device=(attributes.get("device") or "").strip(),
        package_type="img",
        post_install=(attributes.get("post_install") or "").strip(),
        derivative=(attributes.get("derivative") or "").strip(),
    )


def _commented_packages(text: str) -> List[ManifestPackage]:
    """Read the img rows that are commented out in the manifest.

    The catalogue keeps models that are not published in the app yet behind
    XML comments.  Their releases still exist and still need portable
    packages, so onboarding can opt into reading them without changing how
    the app or the website interprets the live manifest.
    """

    packages: List[ManifestPackage] = []
    for comment in _COMMENT_RE.findall(text):
        for tag in _PACKAGE_TAG_RE.findall(comment):
            package = _package_from_attributes(dict(_ATTRIBUTE_RE.findall(tag)))
            if package is not None:
                packages.append(package)
    return packages


def parse_manifest(
    text: str, include_commented: bool = False
) -> List[ManifestPackage]:
    """Parse ``<package type="img">`` rows, skipping comments and APKs.

    ``include_commented`` also returns rows that are commented out, which is
    how the per-model stock ROM repositories are registered before the app
    lists them.  Active rows win when the same row appears both ways.
    """

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"manifest is not valid XML: {exc}") from exc

    packages: List[ManifestPackage] = []
    for element in root.findall("package"):
        package = _package_from_attributes(element.attrib)
        if package is not None:
            packages.append(package)

    if include_commented:
        seen: set = {
            (package.repo.lower(), package.device.upper(), package.name)
            for package in packages
        }
        for package in _commented_packages(text):
            key = (package.repo.lower(), package.device.upper(), package.name)
            if key in seen:
                continue
            seen.add(key)
            packages.append(package)
    return packages


def post_installs_for_source(
    source_repo: str, location: Optional[str] = None
) -> List[str]:
    """Return the manifest's post-install links for one source repository."""

    wanted = source_repo.strip().lower()
    seen: List[str] = []
    for package in parse_manifest(fetch_manifest(location)):
        if package.repo.lower() != wanted or not package.post_install:
            continue
        if package.post_install not in seen:
            seen.append(package.post_install)
    return seen


def _mirror_name(source_repo: str, suffix: str, used: set) -> str:
    owner, name = source_repo.split("/", 1)
    slug = _SAFE_NAME.sub("-", name.lower()).strip("-._") or "firmware"
    suffix = suffix if suffix.startswith("-") else "-" + suffix
    base = slug if slug.endswith(suffix) else slug + suffix
    candidate = base
    if candidate in used:
        candidate = f"{owner.lower()}-{base}".strip("-")
    counter = 2
    original = candidate
    while candidate in used:
        candidate = f"{original}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def group_products(
    packages: Sequence[ManifestPackage],
    org: str = "y1-community",
    suffix: str = "-mirror",
) -> List[Product]:
    """Group manifest rows by source repository and assign mirror names."""

    grouped: Dict[str, List[ManifestPackage]] = {}
    order: List[str] = []
    used: set = set()
    for package in packages:
        key = package.repo.lower()
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(package)

    products: List[Product] = []
    for key in order:
        rows = grouped[key]
        source = rows[0].repo
        mirror = f"{org}/{_mirror_name(source, suffix, used)}"
        products.append(Product(source_repo=source, mirror_repo=mirror, packages=tuple(rows)))
    return products


def product_config(
    product: Product,
    *,
    branch: str = "main",
    updater_repo: str = "y1-community/updater-ce-neo",
    title_prefix: str = "Portable Flashing Package: ",
) -> Dict[str, Any]:
    """Generate a conservative mirror config for one source repository."""

    devices = product.devices or ["Y1"]
    default_model = "Y1" if "Y1" in devices else devices[0]
    # Per-model stock archives (G1, G3, Q5, R1P, ...) are real models, not
    # aliases for Y1.  The metadata must name the model the package restores.
    default_type = "Stock" if default_model != "Y1" else "Type A"

    # Most ROMs use rom.zip for the product default.  The extra rules cover
    # the names used across the manifest: type B, Y1/Y2, and Rockbox's
    # resolution-specific packages.  First match wins.
    rules = [
        {"match_asset": r"(?i)360p", "target_type": "360p"},
        {"match_asset": r"(?i)240p", "target_type": "240p"},
        {
            "match_asset": r"(?i)rom_y1_type[-_ ]?a",
            "target_model": "Y1",
            "target_type": "Type A",
        },
        {
            "match_asset": r"(?i)rom_y1_type[-_ ]?b",
            "target_model": "Y1",
            "target_type": "Type B",
        },
        {
            "match_asset": r"(?i)type[-_ ]?b",
            "target_model": default_model,
            "target_type": "Type B",
        },
        {
            "match_asset": r"(?i)(^|[-_.])y2([-_.]|$)",
            "target_model": "Y2",
            "target_type": "Stock",
        },
    ]

    return {
        "source_repo": product.source_repo,
        "mirror_repo": product.mirror_repo,
        "branch": branch,
        "updater_repo": updater_repo,
        "post_install": product.post_installs,
        "title_prefix": title_prefix,
        "copy_original_assets": True,
        "source_readme": {
            "enabled": True,
            "path": "README.md",
            "commit_message": "Mirror source README",
        },
        "build_workers": 2,
        "max_asset_bytes": 2 * 1024 * 1024 * 1024,
        "releases": {
            "include_prereleases": True,
            "tags": [],
            "exclude_tags": [],
        },
        "defaults": {
            "software_name": product.software_name,
            "target_model": default_model,
            "target_type": default_type,
            "description": "",
        },
        "rules": rules,
    }


def config_text(product: Product, **kwargs: Any) -> str:
    import json

    return json.dumps(product_config(product, **kwargs), indent=2) + "\n"


def caller_workflow(
    packager_repo: str = "ryan-specter/inni-packager",
    packager_ref: str = "main",
) -> str:
    """A small mirror-owned workflow that calls the central reusable one."""

    return f"""name: Portable flashing packages
# managed-by: {packager_repo}

on:
  schedule:
    # Hourly fallback.  The central watcher dispatches this workflow as soon
    # as a source release, seed release or manifest row changes.
    - cron: "17 * * * *"
  repository_dispatch:
    types: [firmware-release, portable-seed]
  workflow_dispatch:
    inputs:
      tags:
        description: "Comma-separated tag globs"
        required: false
        type: string
      platforms:
        description: "Comma-separated platform keys"
        required: false
        type: string
      limit:
        description: "Only the N newest releases"
        required: false
        type: string
      offset:
        description: "Skip the N newest releases"
        required: false
        type: string
      force:
        description: "Rebuild releases that are already current"
        required: false
        type: boolean
        default: false
      dry_run:
        description: "Build without publishing"
        required: false
        type: boolean
        default: false
      mode:
        description: "sync, repack (mirror's stored firmware) or index (rebuild index.json)"
        required: false
        type: string
        default: "sync"
      shards:
        description: "Split the backfill across this many parallel jobs (1 = one job)"
        required: false
        type: string
        default: "1"

permissions:
  contents: write

concurrency:
  group: portable-flashing-packages
  cancel-in-progress: false

jobs:
  mirror:
    uses: {packager_repo}/.github/workflows/reusable-mirror.yml@{packager_ref}
    with:
      tags: ${{{{ inputs.tags || '' }}}}
      platforms: ${{{{ inputs.platforms || '' }}}}
      limit: ${{{{ inputs.limit || '' }}}}
      offset: ${{{{ inputs.offset || '' }}}}
      force: ${{{{ inputs.force || false }}}}
      dry_run: ${{{{ inputs.dry_run || false }}}}
      mode: ${{{{ inputs.mode || 'sync' }}}}
      shards: ${{{{ inputs.shards || '1' }}}}
    secrets: inherit
"""
