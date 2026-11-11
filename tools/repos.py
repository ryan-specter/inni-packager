"""Repository and domain fallbacks shared by the packager.

A repository is normally published under the ``y1-community`` organisation,
but every read path also has to survive that repository being moved to the
``ryan-specter`` account.  The public site is the final fallback for content
that also exists at ``innioasis.app``.

Only read paths use these helpers.  Writes still target the repository the
caller configured, so a fallback can never turn into an accidental push to a
different account.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

PRIMARY_OWNER = "y1-community"
FALLBACK_OWNER = "ryan-specter"
SITE = "https://innioasis.app"
GITHUB = "https://github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"
API_GITHUB = "https://api.github.com"

_MANIFEST_NAME = "slidia_manifest.xml"
_MANIFEST_BRANCH = "main"
_MANIFEST_REPO = "Innioasis-Updater"
_CONFIG_NAME = "config.ini"

_PAT_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "github_token_")


def split_repo(repo: str) -> Tuple[str, str]:
    """Return ``(owner, name)`` for an ``owner/name`` repository string."""

    clean = str(repo or "").strip().strip("/")
    if "/" not in clean:
        return "", clean
    owner, name = clean.split("/", 1)
    return owner, name


def fallback_repo(repo: str) -> str:
    """The ``ryan-specter`` spelling of a ``y1-community`` repository."""

    owner, name = split_repo(repo)
    if not owner or not name or owner.lower() != PRIMARY_OWNER:
        return ""
    return FALLBACK_OWNER + "/" + name


def repo_variants(repo: str) -> List[str]:
    """A repository and its fallback spelling, without duplicates."""

    candidates: List[str] = []
    for candidate in (str(repo or "").strip().strip("/"), fallback_repo(repo)):
        if candidate and candidate.lower() not in {item.lower() for item in candidates}:
            candidates.append(candidate)
    return candidates


def url_variants(url: str) -> List[str]:
    """Every GitHub URL plus the same path under the fallback owner."""

    value = str(url or "")
    candidates = [value]
    if value:
        fallback = re.sub(
            r"(?i)(github\.com|raw\.githubusercontent\.com|api\.github\.com)/(y1-community)/",
            lambda match: match.group(1) + "/" + FALLBACK_OWNER + "/",
            value,
        )
        if fallback != value:
            candidates.append(fallback)
    return candidates


def asset_url_variants(url: str) -> List[str]:
    """Release-asset URL variants, primary owner first."""

    return url_variants(url)


def release_asset_url(repo: str, tag: str, name: str) -> str:
    """The stable browser URL for one release asset."""

    return (
        f"{GITHUB}/{str(repo).strip('/')}/releases/download/"
        f"{urllib.parse.quote(str(tag), safe='')}/"
        f"{urllib.parse.quote(str(name), safe='')}"
    )


def release_asset_urls(repo: str, tag: str, name: str) -> List[str]:
    """Primary and fallback URLs for a release asset."""

    return [
        release_asset_url(candidate, tag, name)
        for candidate in repo_variants(repo)
    ]


def raw_blob_url(repo: str, branch: str, path: str) -> str:
    """A plain raw.githubusercontent.com download, not an API request."""

    return (
        f"{RAW_GITHUB}/{str(repo).strip('/')}/{urllib.parse.quote(str(branch), safe='')}/"
        f"{urllib.parse.quote(str(path).lstrip('/'), safe='/')}"
    )


def raw_blob_urls(repo: str, branch: str, path: str) -> List[str]:
    return [raw_blob_url(candidate, branch, path) for candidate in repo_variants(repo)]


def manifest_urls() -> List[str]:
    """GitHub first, then the public site, for ``slidia_manifest.xml``."""

    paths = [
        raw_blob_url(f"{PRIMARY_OWNER}/{_MANIFEST_REPO}", _MANIFEST_BRANCH, _MANIFEST_NAME),
        raw_blob_url(f"{FALLBACK_OWNER}/{_MANIFEST_REPO}", _MANIFEST_BRANCH, _MANIFEST_NAME),
        site_url(_MANIFEST_NAME),
    ]
    return _dedupe(paths)


def config_ini_urls() -> List[str]:
    """GitHub copies first, with the public site last.

    ``config.ini`` normally lives beside the site checkout rather than in a
    repository, because it can contain read-only PATs.  Callers should try the
    local file before these URLs; this list is only for a deployment where the
    file is published deliberately.
    """

    paths = [
        raw_blob_url(f"{PRIMARY_OWNER}/{_MANIFEST_REPO}", _MANIFEST_BRANCH, _CONFIG_NAME),
        raw_blob_url(f"{FALLBACK_OWNER}/{_MANIFEST_REPO}", _MANIFEST_BRANCH, _CONFIG_NAME),
        site_url(_CONFIG_NAME),
    ]
    return _dedupe(paths)


def site_url(path: str = "") -> str:
    return SITE + "/" + str(path or "").lstrip("/")


def site_urls(path: str = "") -> List[str]:
    """The site URL, then its repository copy when callers need one."""

    return [site_url(path)]


def normalize_pat(value: str) -> str:
    """Accept a GitHub token with or without its ``github_pat_`` prefix."""

    token = str(value or "").strip().strip("\"'")
    if not token:
        return ""
    if not token.lower().startswith(_PAT_PREFIXES):
        token = "github_pat_" + token
    return token


def tokens_from_ini(text: str) -> List[str]:
    """Read ``key_N`` and ``token`` values from a config.ini body."""

    tokens: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not re.match(r"^(key_\d+|token)\s*=", line, flags=re.IGNORECASE):
            continue
        value = line.split("=", 1)[1].strip().strip("\"'")
        token = normalize_pat(value)
        if len(token) > 10 and token not in tokens:
            tokens.append(token)
    return tokens


def config_paths(root: Path) -> List[Path]:
    """Local config.ini locations, nearest first."""

    root = Path(root)
    return [root / _CONFIG_NAME, root.parent / _CONFIG_NAME]


def _dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


__all__ = [
    "API_GITHUB",
    "FALLBACK_OWNER",
    "GITHUB",
    "PRIMARY_OWNER",
    "RAW_GITHUB",
    "SITE",
    "asset_url_variants",
    "config_ini_urls",
    "config_paths",
    "fallback_repo",
    "manifest_urls",
    "normalize_pat",
    "raw_blob_url",
    "raw_blob_urls",
    "release_asset_url",
    "release_asset_urls",
    "repo_variants",
    "site_url",
    "site_urls",
    "split_repo",
    "tokens_from_ini",
    "url_variants",
]
