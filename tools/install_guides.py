"""Install guides for the flash-tool-only (legacy) packages.

A legacy package carries MediaTek's SP Flash Tool on its own, with the firmware
already in the tool folder.  It exists because Updater CE cannot run on those
computers:

* Windows XP, Vista, 7, 8 and 8.1, and 32-bit Windows
* 32-bit Linux, including distributions older than the AppImage builds

One renderer produces every guide:

* ``render_text`` writes the ``README.txt`` inside each portable package,
  parameterised by the software name, version, model, hardware type, firmware
  file and scatter that package carries -- so every brand/model/firmware
  permutation gets its own instructions.
* ``render_markdown`` writes the static guide pages committed under
  ``docs/guides/legacy`` (``python tools/install_guides.py`` regenerates them).
* ``guide_url`` gives the website helper the link for a detected legacy
  system, so those users are sent to their own guide rather than the Updater CE
  guide.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

GUIDE_BASE = "https://innioasis.app/install/"
UPDATER_GUIDE = "https://innioasis.app/guide.html"

# The two systems that install from the portable zip instead of Updater CE.
LEGACY_PLATFORMS: Dict[str, Dict[str, str]] = {
    "windows-legacy": {
        "slug": "legacy-windows",
        "title": "Windows XP / Vista / 7 / 8 / 8.1 and 32-bit Windows",
        "heading": "Innioasis Firmware + SP Flash Tool (Windows 7 and older)",
        "tool": "SP_Flash_Tool",
        "launcher": "flash_tool.exe",
        "short": "Windows 7 / XP / 8.1 (SP Flash Tool)",
    },
    "linux-legacy": {
        "slug": "legacy-linux-32",
        "title": "32-bit Linux (pre-AppImage SP Flash Tool)",
        "heading": "Innioasis Firmware + SP Flash Tool (32-bit Linux)",
        "tool": "SP_Flash_Tool",
        "launcher": "./flash_tool",
        "short": "32-bit Linux (SP Flash Tool)",
    },
}


def platform_keys() -> List[str]:
    """Legacy platform keys in their display order."""

    return list(LEGACY_PLATFORMS)


def _meta(platform_key: str) -> Dict[str, str]:
    try:
        return LEGACY_PLATFORMS[platform_key]
    except KeyError as exc:
        raise KeyError(f"no legacy install guide for {platform_key!r}") from exc


def headline(
    software: str = "", version: str = "", model: str = "", target_type: str = ""
) -> str:
    """One line naming the exact product permutation."""

    software_line = " ".join(part for part in (software, version) if part).strip()
    device = " ".join(part for part in (model, target_type) if part).strip()
    if software_line and device:
        return f"{software_line} - {device}"
    return software_line or device or "Innioasis firmware"


def _extract_step(platform_key: str) -> Tuple[str, List[str]]:
    if platform_key == "linux-legacy":
        return (
            "Extract the download",
            [
                "Extract the zip into a normal folder, for example ~/Innioasis.",
                "Do not run the Flash Tool from inside the zip viewer.",
            ],
        )
    return (
        "Extract the download",
        [
            "Right-click the zip, choose Extract All, and extract it to a normal",
            "folder. Do not run the Flash Tool from inside the zip window.",
        ],
    )


def _driver_step(platform_key: str) -> Tuple[str, List[str]]:
    if platform_key == "linux-legacy":
        return (
            "Install the driver rules",
            [
                "The package keeps MediaTek's USB rules in the SP_Flash_Tool",
                "folder (99-ttyacms.rules). Copy that file into",
                "/etc/udev/rules.d/ and reload the rules before opening the tool.",
            ],
        )
    return (
        "Install the driver first",
        [
            "The package includes the MediaTek USB driver installer in the",
            "Driver folder as 1.Install.Driver.First.and.Reboot.PC.exe.",
            "Run it before opening the Flash Tool. Windows 7 cannot see the",
            "player without it.",
        ],
    )


def _launch_step(platform_key: str, scatter: str) -> Tuple[str, List[str]]:
    meta = _meta(platform_key)
    if platform_key == "linux-legacy":
        return (
            "Start the Flash Tool",
            [
                f"Open the {meta['tool']} folder and run {meta['launcher']}. This is",
                "the 32-bit build, so a 64-bit distribution needs its 32-bit runtime",
                "libraries (for example libpng12.so.0 and libusb-1.0.so.0) from the",
                "multilib packages.",
                f"The firmware scatter ({scatter}) and the download agent are already",
                "selected.",
            ],
        )
    return (
        "Start the Flash Tool",
        [
            f"Open the {meta['tool']} folder and run {meta['launcher']}. The firmware",
            f"scatter ({scatter}) and the download agent are already selected.",
        ],
    )


def steps(
    platform_key: str, scatter: str = "", firmware_asset: str = ""
) -> List[Tuple[str, List[str]]]:
    """The numbered guide, shared by the package README and the static pages."""

    _meta(platform_key)
    scatter = scatter or "Android_scatter.txt"
    firmware = firmware_asset or "the firmware zip"
    return [
        _extract_step(platform_key),
        _driver_step(platform_key),
        ("Reboot the PC", ["Restart the computer and open the extracted folder again."]),
        _launch_step(platform_key, scatter),
        (
            "Click Download while the player is off",
            [
                "Keep the player powered off and disconnected. Click Download in the",
                "Flash Tool, then connect the player with a USB cable that carries",
                "data.",
            ],
        ),
        (
            "Wait for the completion message",
            [
                "The tool ends with a green check and a completed message. Do not",
                "unplug the player before it appears. If nothing happens, hold the",
                "power button for ten seconds, disconnect, and repeat from the",
                "Download step.",
            ],
        ),
        (
            "If the player is still not detected",
            [
                "Reinstall the driver, restart the computer, and try another USB",
                "port and a different data cable.",
            ],
        ),
    ]


def _footer_lines(platform_key: str) -> List[str]:
    meta = _meta(platform_key)
    return [
        "This guide is only for computers that cannot run Innioasis Updater CE.",
        "Windows 10 and 11, 64-bit Linux and macOS should use the Updater CE",
        f"package instead: {UPDATER_GUIDE}",
        "",
        f"These steps are the same for every {meta['title']} package; the",
        "README in your download names your exact model, hardware type and",
        "firmware file.",
    ]


def render_text(
    platform_key: str = "windows-legacy",
    *,
    software: str = "",
    version: str = "",
    model: str = "",
    target_type: str = "",
    firmware_asset: str = "",
    scatter: str = "",
    tag: str = "",
) -> str:
    """Plain-text README for one legacy package (CRLF, as Windows expects)."""

    meta = _meta(platform_key)
    lines = [
        meta["heading"],
        "=" * len(meta["heading"]),
        "",
        f"Firmware: {headline(software, version, model, target_type)}",
        f"Package: {firmware_asset or 'firmware zip'}",
        "",
        "How to install the firmware:",
        "",
    ]
    for index, (title, body) in enumerate(
        steps(platform_key, scatter, firmware_asset), start=1
    ):
        lines.append(f"{index}. {title}")
        for line in body:
            lines.append(f"   {line}")
        lines.append("")
    lines.extend(_footer_lines(platform_key))
    lines += [
        "",
        f"Guide for this package: {guide_url(platform_key, model=model, target_type=target_type, tag=tag, software=software)}",
        "",
        "donate.txt lists the ways to support the projects behind this firmware.",
        "",
    ]
    return "\r\n".join(lines)


def render_markdown(
    platform_key: str = "windows-legacy",
    *,
    software: str = "",
    version: str = "",
    model: str = "",
    target_type: str = "",
    firmware_asset: str = "",
    scatter: str = "",
    tag: str = "",
) -> str:
    """Static guide page body for the website build."""

    meta = _meta(platform_key)
    lines = [
        f"# {meta['heading']}",
        "",
        f"**Firmware:** {headline(software, version, model, target_type)}  ",
        f"**Package:** {firmware_asset or 'firmware zip'}  ",
        f"**Applies to:** {meta['title']}",
        "",
        "## How to install the firmware",
        "",
    ]
    for index, (title, body) in enumerate(
        steps(platform_key, scatter, firmware_asset), start=1
    ):
        lines.append(f"### {index}. {title}")
        lines.append("")
        lines.extend(body)
        lines.append("")
    lines.extend(_footer_lines(platform_key))
    lines += [
        "",
        f"[Open this guide for your device]({guide_url(platform_key, model=model, target_type=target_type, tag=tag, software=software)})",
        "",
    ]
    return "\n".join(lines)


def legacy_readme(
    software: str,
    version: str,
    firmware_asset: str,
    scatter: str,
    platform_key: str = "windows-legacy",
    model: str = "",
    target_type: str = "",
    tag: str = "",
) -> str:
    """Compatibility wrapper used by the packager."""

    return render_text(
        platform_key,
        software=software,
        version=version,
        firmware_asset=firmware_asset,
        scatter=scatter,
        model=model,
        target_type=target_type,
        tag=tag,
    )


def guide_url(
    platform_key: str,
    *,
    model: str = "",
    target_type: str = "",
    tag: str = "",
    software: str = "",
) -> str:
    """Static page for one system, carrying the device permutation."""

    if platform_key not in LEGACY_PLATFORMS:
        return ""
    params = [
        (key, value)
        for key, value in (
            ("software", software),
            ("model", model),
            ("type", target_type),
            ("tag", tag),
        )
        if value
    ]
    query = f"?{urlencode(params)}" if params else ""
    return f"{GUIDE_BASE}{LEGACY_PLATFORMS[platform_key]['slug']}.html{query}"


def static_pages() -> Dict[str, str]:
    """The committed static guide pages, keyed by filename."""

    pages: Dict[str, str] = {
        "legacy-windows.md": render_markdown("windows-legacy"),
        "legacy-linux-32.md": render_markdown("linux-legacy"),
    }
    index = [
        "# Legacy install guides",
        "",
        "These are the static guides for computers that cannot run Updater CE.",
        "The site shows only the guide that matches the detected system, and",
        "always offers the other download options as well.",
        "",
        "- [Windows XP / Vista / 7 / 8 / 8.1 and 32-bit Windows](legacy-windows.md)",
        "- [32-bit Linux (pre-AppImage SP Flash Tool)](legacy-linux-32.md)",
        "",
    ]
    pages["index.md"] = "\n".join(index)
    return pages


def write_static_guides(directory: str | Path) -> List[Path]:
    """Write the static pages and return their paths."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for name, text in static_pages().items():
        path = root / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the static legacy install guides."
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "docs" / "guides" / "legacy"),
    )
    args = parser.parse_args(argv)
    for path in write_static_guides(args.out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
