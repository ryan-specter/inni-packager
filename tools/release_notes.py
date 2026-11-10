"""Image-button release-note headers for mirrored releases.

GitHub release bodies cannot use stylesheet-styled buttons, so each button is
a small SVG hosted from the mirror repository and wrapped in a normal link.
The images are deliberately self-contained: no external icon font, script or
hosted stylesheet is needed, and the same notes work in GitHub's Markdown
renderer.

Release-asset downloads are served as ``application/octet-stream`` by GitHub,
which browsers will not render inside an ``<img>``.  The SVGs therefore live
under ``.mirror-assets/`` on the mirror's branch, where
``raw.githubusercontent.com`` serves them as ``image/svg+xml``.
"""

from __future__ import annotations

from collections import OrderedDict
from html import escape
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import quote, urlencode

from mirrorspec import MirrorConfig, strip_block

INSTALL_GUIDE_BASE = "https://innioasis.app/guide.html"
BUTTON_PREFIX = "release-button-"
REPOSITORY_BUTTON_DIR = ".mirror-assets"
BUTTON_WIDTH = 520
BUTTON_HEIGHT = 116

# Order is intentional: Windows x64, Windows arm64, Linux x64, Linux arm64,
# macOS.  The last two are optional upstream, so a release simply omits them
# when the corresponding portable package was not built.
BUTTONS: "OrderedDict[str, Tuple[str, str]]" = OrderedDict(
    (
        ("windows", ("Download .zip for Windows", "x86-64 · portable package")),
        ("windows-arm64", ("Download .zip for Windows arm64", "portable package")),
        (
            "windows-legacy",
            ("Download .zip for Windows XP/7/8", "32/64-bit · SP Flash Tool"),
        ),
        ("linux", ("Download .zip for Linux", "x86-64 · portable package")),
        ("linux-arm64", ("Download .zip for Linux arm64", "portable package")),
        (
            "macos",
            ("Download .zip for macOS 12+", "Intel + Apple silicon"),
        ),
    )
)
UPDATER_BUTTON = ("Get it on Updater CE", "with guided install")


def button_filename(key: str) -> str:
    """Filename used for one generated button asset."""

    return f"{BUTTON_PREFIX}{key}.svg"


HEADER_MARKER = ".mirror-assets/"
SECOND_LEVEL_HEADING = "### Downloads"


def strip_generated_header(body: str) -> str:
    """Return the original source notes from a mirrored release body.

    A mirrored body is ``generated header + source notes + hidden block``.
    Re-packing an existing mirror release needs the notes on their own,
    otherwise the regenerated header and the previous build record would be
    nested inside the next body.  The header is recognised by its centred
    button images and its trailing rule; anything else is returned untouched.
    """

    text = strip_block(body)
    if not text.startswith('<p align="center">'):
        return text
    marker = text.rfind(HEADER_MARKER)
    if marker == -1 or SECOND_LEVEL_HEADING not in text:
        return text
    cut = text.find("\n---\n", marker)
    if cut == -1:
        return text
    return text[cut + len("\n---\n") :].strip()


def _svg_text(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _icon(key: str) -> str:
    if key == "updater":
        return """
  <rect x="16" y="17" width="62" height="62" rx="15" fill="#182538" stroke="#334963"/>
  <rect x="25" y="23" width="44" height="36" rx="8" fill="#f5c54d"/>
  <rect x="31" y="29" width="32" height="21" rx="4" fill="#644d12"/>
  <circle cx="47" cy="60" r="4" fill="#fff7d6"/>
  <path d="M34 38h25M34 44h16" stroke="#f2d37a" stroke-width="2" stroke-linecap="round"/>
"""
    return """
  <rect x="16" y="17" width="62" height="62" rx="15" fill="#172b42" stroke="#2f5580"/>
  <circle cx="47" cy="48" r="20" fill="#142235" stroke="#3c74ad" stroke-width="2"/>
  <path d="M47 32v24m0 0l-8-8m8 8l8-8M33 62h28" fill="none" stroke="#79bdff"
        stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
"""


def button_svg(key: str) -> bytes:
    """Return a standalone, dark card-style SVG for one download/Updater button."""

    if key == "updater":
        title, subtitle = UPDATER_BUTTON
    elif key in BUTTONS:
        title, subtitle = BUTTONS[key]
    else:
        raise KeyError(f"unknown release-note button: {key}")

    label = f"{title} - {subtitle}"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{BUTTON_WIDTH}" height="{BUTTON_HEIGHT}"
     viewBox="0 0 {BUTTON_WIDTH} {BUTTON_HEIGHT}" role="img" aria-label="{_svg_text(label)}">
  <defs>
    <linearGradient id="card" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#101b29"/>
      <stop offset="1" stop-color="#0b121c"/>
    </linearGradient>
    <linearGradient id="edge" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#35506e" stop-opacity=".2"/>
      <stop offset=".5" stop-color="#5b86b5" stop-opacity=".7"/>
      <stop offset="1" stop-color="#35506e" stop-opacity=".2"/>
    </linearGradient>
  </defs>
  <rect x="1" y="1" width="518" height="114" rx="19" fill="url(#card)" stroke="#2a3b50"/>
  <path d="M22 1h476" stroke="url(#edge)" stroke-width="2"/>
{_icon(key)}
  <text x="96" y="49" fill="#f8fafc" font-family="Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
        font-size="22" font-weight="700" letter-spacing="-.2">{_svg_text(title)}</text>
  <text x="96" y="78" fill="#9fb0c7" font-family="Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
        font-size="16" font-weight="400">{_svg_text(subtitle)}</text>
</svg>
"""
    return svg.encode("utf-8")


def button_assets(platform_keys: Iterable[str]) -> Dict[str, bytes]:
    """Return the Updater button plus every requested platform button."""

    keys = ["updater"]
    for key in BUTTONS:
        if key in platform_keys and key not in keys:
            keys.append(key)
    return {button_filename(key): button_svg(key) for key in keys}


def write_button_assets(directory: Any, platform_keys: Iterable[str]) -> List[Any]:
    """Write button SVGs into ``directory`` and return their paths."""

    from pathlib import Path

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for name, data in button_assets(platform_keys).items():
        path = root / name
        path.write_bytes(data)
        paths.append(path)
    return paths


def button_url(cfg: MirrorConfig, name: str) -> str:
    """Public URL for a generated button hosted on the mirror's branch."""

    if not cfg.mirror_repo:
        return "#"
    branch = quote(cfg.branch or "main", safe="/")
    return (
        f"https://raw.githubusercontent.com/{cfg.mirror_repo}/{branch}/"
        f"{REPOSITORY_BUTTON_DIR}/{quote(name, safe='')}"
    )


def install_guide_url(cfg: MirrorConfig, release: Mapping[str, Any]) -> str:
    """Link the Updater button to the guide for this source release.

    The live guide is still the generic install page, but carries the source
    repository and tag in its query string.  That makes the link specific
    enough for the site to deep-link a guide in the future without changing
    already-published release notes.
    """

    tag = str(release.get("tag_name") or release.get("tag") or "")
    params = {"repo": cfg.source_repo}
    if tag:
        params["tag"] = tag
    return f"{INSTALL_GUIDE_BASE}?{urlencode(params)}#download-updater"


def _device_label(
    asset: Mapping[str, Any], cfg: MirrorConfig
) -> Tuple[str, str]:
    model = str(asset.get("model") or cfg.target_model or "").strip()
    target_type = str(asset.get("type") or cfg.target_type or "").strip()
    if not model:
        model = "All devices"
    return model, target_type


def _asset_url(cfg: MirrorConfig, tag: str, name: str) -> str:
    if not cfg.mirror_repo:
        return "#"
    return (
        f"https://github.com/{cfg.mirror_repo}/releases/download/"
        f"{quote(tag, safe='')}/{quote(name, safe='')}"
    )


def _image_url(cfg: MirrorConfig, tag: str, name: str) -> str:
    # release-asset URLs are application/octet-stream on GitHub; the copy on
    # the branch is served with a real image content type.
    return button_url(cfg, name)


def _attr(value: str) -> str:
    return escape(str(value), {"&": "&amp;", '"': "&quot;"})


def _download_button(
    cfg: MirrorConfig,
    tag: str,
    item: Mapping[str, Any],
    platform: str,
    device_label: str,
) -> str:
    title, subtitle = BUTTONS[platform]
    href = _asset_url(cfg, tag, str(item.get("name") or ""))
    image = _image_url(cfg, tag, button_filename(platform))
    alt = f"{title} for {device_label} - {subtitle}"
    return (
        f'<a href="{_attr(href)}"><img src="{_attr(image)}" '
        f'width="400" alt="{_attr(alt)}"></a>'
    )


def render_header(
    cfg: MirrorConfig,
    release: Mapping[str, Any],
    block: Mapping[str, Any],
) -> str:
    """Build the visible image-button header placed before source notes."""

    tag = str(release.get("tag_name") or release.get("tag") or "")
    updater_href = install_guide_url(cfg, release)
    updater_image = _image_url(cfg, tag, button_filename("updater"))
    updater_alt = f"{UPDATER_BUTTON[0]} - {UPDATER_BUTTON[1]}"
    lines = [
        '<p align="center">',
        f'  <a href="{_attr(updater_href)}"><img src="{_attr(updater_image)}" '
        f'width="400" alt="{_attr(updater_alt)}"></a>',
        "</p>",
        "",
        "### Downloads",
        "",
    ]

    groups: "OrderedDict[Tuple[str, str], List[Mapping[str, Any]]]" = OrderedDict()
    for item in block.get("assets") or []:
        if not isinstance(item, Mapping):
            continue
        platform = str(item.get("platform") or "")
        if platform not in BUTTONS:
            continue
        model, target_type = _device_label(item, cfg)
        groups.setdefault((model, target_type), []).append(item)

    for (model, target_type), items in groups.items():
        device_label = model
        if target_type:
            device_label += f" · {target_type}"
        if len(groups) > 1:
            lines.append(f"#### {device_label}")
            lines.append("")
        else:
            lines.append(f"**{device_label}**")
            lines.append("")

        by_platform: Dict[str, Mapping[str, Any]] = {}
        for item in items:
            platform = str(item.get("platform") or "")
            if platform in BUTTONS and platform not in by_platform:
                by_platform[platform] = item

        ordered = [key for key in BUTTONS if key in by_platform]
        for index in range(0, len(ordered), 2):
            row = ordered[index : index + 2]
            lines.append('<p align="center">')
            for platform in row:
                lines.append(
                    "  "
                    + _download_button(
                        cfg, tag, by_platform[platform], platform, device_label
                    )
                )
            lines.append("</p>")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n\n---\n"
