"""Turn a product's manifest ``post_install`` links into a package file.

The manifest points at either a Markdown document or the vendor's web page.
Markdown becomes a plain-text ``3. After Install Steps.txt`` so it opens on
every OS.  A web-only link becomes the platform's normal internet shortcut.
"""

from __future__ import annotations

import plistlib
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

FILE_STEM = "3. After Install Steps"
GITHUB_BLOB = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", re.IGNORECASE
)
RAW_GITHUB = re.compile(
    r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$",
    re.IGNORECASE,
)
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown")
REQUEST_HEADERS = {"User-Agent": "inni-packager/1.0"}


@dataclass(frozen=True)
class PostInstall:
    """The links and any Markdown fetched for one product."""

    urls: Tuple[str, ...]
    markdown: Tuple[Tuple[str, str], ...] = ()

    def render(self, platform_key: str) -> Optional[Tuple[str, bytes]]:
        """Return ``(filename, contents)`` for one platform, if there is work."""

        if not self.urls:
            return None
        if self.markdown:
            parts: List[str] = []
            for url, text in self.markdown:
                parts.append(f"Source: {url}\n\n{text.strip()}\n")
            linked = [url for url in self.urls if url not in {u for u, _ in self.markdown}]
            if linked:
                parts.append(
                    "More information:\n"
                    + "\n".join(f"- {url}" for url in linked)
                    + "\n"
                )
            return (
                FILE_STEM + ".txt",
                "\n\n---\n\n".join(parts).encode("utf-8"),
            )
        if len(self.urls) == 1:
            url = self.urls[0]
            if platform_key == "windows":
                return (
                    FILE_STEM + ".url",
                    ("[InternetShortcut]\r\nURL=" + url + "\r\n").encode("utf-8"),
                )
            if platform_key.startswith("linux"):
                return (
                    FILE_STEM + ".desktop",
                    (
                        "[Desktop Entry]\n"
                        "Type=Link\n"
                        "Name=After Install Steps\n"
                        f"URL={url}\n"
                        "Icon=text-html\n"
                    ).encode("utf-8"),
                )
            if platform_key == "macos":
                return (
                    FILE_STEM + ".webloc",
                    plistlib.dumps({"URL": url}, fmt=plistlib.FMT_XML),
                )
            return (
                FILE_STEM + ".url",
                ("[InternetShortcut]\r\nURL=" + url + "\r\n").encode("utf-8"),
            )
        return (
            FILE_STEM + ".txt",
            (
                "After Install Steps\n"
                "===================\n\n"
                + "\n".join(f"- {url}" for url in self.urls)
                + "\n"
            ).encode("utf-8"),
        )


def _is_markdown(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    return path.endswith(MARKDOWN_SUFFIXES)


def _download(url: str, timeout: int = 45) -> str:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _markdown_text(url: str, gh: Any, timeout: int = 45) -> Optional[str]:
    """Fetch Markdown from GitHub's contents API or the public URL."""

    blob = GITHUB_BLOB.match(url)
    if blob:
        owner, repo, ref, path = blob.groups()
        try:
            found = gh.get_file(f"{owner}/{repo}", unquote(path), ref=unquote(ref))
        except Exception:
            found = None
        if found and found.get("text"):
            return str(found["text"])
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    else:
        raw_match = RAW_GITHUB.match(url)
        raw = url if not raw_match else url
    try:
        return _download(raw, timeout=timeout)
    except Exception:
        return None


def resolve(
    urls: Sequence[str],
    gh: Any,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[PostInstall]:
    """Resolve unique links and prefetch Markdown once per product."""

    unique: List[str] = []
    for raw in urls:
        url = str(raw or "").strip()
        if url and url not in unique:
            unique.append(url)
    if not unique:
        return None
    fetched: List[Tuple[str, str]] = []
    for url in unique:
        if not _is_markdown(url):
            continue
        text = _markdown_text(url, gh)
        if text:
            fetched.append((url, text))
        elif log:
            log(f"  warning: could not fetch post-install Markdown {url}")
    return PostInstall(tuple(unique), tuple(fetched))
