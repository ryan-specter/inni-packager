"""Repackage firmware zips inside an Innioasis Updater CE portable zip.

The output keeps every entry of the portable application zip (including macOS
symlinks and permission bits) and adds the selected scatter, the images it
lists and an optional updater image from the firmware zip:

* ``sp_flash_tool`` layout (Windows/Linux): those firmware files go directly
  into the portable's ``SP_Flash_Tool`` folder, a ``fw_package.ini`` is written
  beside the scatter and ``history.ini`` is rewritten so SP Flash Tool preloads
  it.
* ``beside_app`` layout (macOS): the portable app is renamed
  ``Install Firmware.app`` and the firmware plus ``fw_package.ini`` go into a
  neighbouring ``Firmware`` folder.

Surrounding files from the firmware zip, such as a bundled SP Flash Tool tree,
are stripped; only what the scatter declares is needed.

Everything is written with maximum deflate.  No file is buffered whole in
memory; large partition images stream through a 1 MiB window.
"""

from __future__ import annotations

import hashlib
import plistlib
import posixpath
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from mirrorspec import Platform, drops_preserve_txt, macos_readme

BUFFER_SIZE = 1024 * 1024
COMPRESS_LEVEL = 9
DRIVE_RE = re.compile(r"^[A-Za-z]:")
MAC_APP_NAME = "Install Firmware.app"
MAC_FIRMWARE_DIR = "Firmware"
PACKAGE_IMAGE_NAMES = ("updater.png", "updater.jpg", "updater.jpeg")

HISTORY_INI = (
    "[LastDAFilePath]\r\n"
    "lastDir=MTK_AllInOne_DA.bin\r\n"
    "\r\n"
    "[RecentOpenFile]\r\n"
    "lastDir=\r\n"
    "scatterHistory={scatter}\r\n"
    "authHistory=@Invalid()\r\n"
)


class PackagingError(RuntimeError):
    pass


@dataclass
class FirmwarePackage:
    path: Path
    scatter_dir: str
    scatters: List[str]
    chosen_scatter: Optional[str]
    entry_count: int

    def describe(self) -> str:
        return (
            f"{self.path.name}: {self.entry_count} entries, "
            f"scatter={self.chosen_scatter or '(multiple/none)'}"
        )


@dataclass
class BuildResult:
    path: Path
    size: int
    sha256: str
    source_asset: str
    platform: str
    entries: int
    skipped_entries: List[str] = field(default_factory=list)


def _safe_name(raw: str) -> Optional[str]:
    """Normalise a zip entry name and reject absolute/traversal paths."""

    name = str(raw).replace("\\", "/")
    is_dir = name.endswith("/")
    if name.startswith("/") or DRIVE_RE.match(name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if not parts:
        return None
    clean = "/".join(parts)
    return clean + ("/" if is_dir else "")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _safe_date_time(info: zipfile.ZipInfo) -> Tuple[int, int, int, int, int, int]:
    try:
        year, month, day, hour, minute, second = info.date_time
    except (TypeError, ValueError):
        return (1980, 1, 1, 0, 0, 0)
    if year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    month = min(max(month, 1), 12)
    day = min(max(day, 1), 31)
    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    second = min(max(second, 0), 59)
    return (year, month, day, hour, minute, second)


def _copy_info(
    dst: zipfile.ZipFile,
    src: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    name: str,
    compress_type: int,
) -> None:
    """Copy one entry, preserving type, timestamp and permission bits."""

    target = zipfile.ZipInfo(name, _safe_date_time(info))
    target.external_attr = info.external_attr
    target.internal_attr = info.internal_attr
    target.create_system = info.create_system
    target.comment = info.comment
    target.compress_type = compress_type
    # Declare the size so zipfile selects ZIP64 for entries that expand past
    # 4 GiB.  Large sparse partition images inside a firmware zip do, and
    # streaming them into a non-ZIP64 entry fails with "File size too large,
    # try using force_zip64".
    target.file_size = info.file_size

    if name.endswith("/") or _is_symlink(info):
        # Directories and symlinks are stored: a symlink's "content" is its
        # target path, and deflating it would not help extraction.
        dst.writestr(target, src.read(info))
        return
    with src.open(info) as reader, dst.open(
        target, "w", force_zip64=target.file_size * 1.05 > zipfile.ZIP64_LIMIT
    ) as writer:
        shutil.copyfileobj(reader, writer, BUFFER_SIZE)


def inspect_firmware(path: Path, source_repo: str = "") -> FirmwarePackage:
    """Find the scatter file and its folder inside a firmware zip."""

    try:
        with zipfile.ZipFile(path) as zf:
            names = [_safe_name(i.filename) for i in zf.infolist()]
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackagingError(f"{path.name} is not a readable zip: {exc}") from exc
    clean_names = [n for n in names if n]
    scatters = [
        n
        for n in clean_names
        if not n.endswith("/")
        and n.lower().endswith(".txt")
        and "scatter" in posixpath.basename(n).lower()
    ]
    # Preserve-data variants are alternative layouts, not the package scatter.
    scatters = [
        n for n in scatters if "preserve" not in posixpath.basename(n).lower()
    ]
    if not scatters:
        return FirmwarePackage(path, "", [], None, len(clean_names))

    groups: Dict[str, List[str]] = {}
    for name in scatters:
        groups.setdefault(posixpath.dirname(name), []).append(name)

    # Prefer a scatter at the zip root; otherwise the folder with the most
    # scatter files (a full package sometimes ships preserve_data variants).
    if "" in groups:
        scatter_dir = ""
    else:
        scatter_dir = sorted(
            groups.items(), key=lambda item: (-len(item[1]), item[0].count("/"), item[0])
        )[0][0]

    basenames = [posixpath.basename(n) for n in groups[scatter_dir]]
    chosen: Optional[str] = None
    non_preserve = [n for n in basenames if "preserve_data" not in n.lower()]
    android = [n for n in non_preserve if "ndroid_scatter" in n.lower()]
    if len(android) == 1:
        chosen = android[0]
    elif len(non_preserve) == 1:
        chosen = non_preserve[0]
    elif len(basenames) == 1:
        chosen = basenames[0]

    return FirmwarePackage(path, scatter_dir, basenames, chosen, len(clean_names))


def _tool_layout(tool_zip: zipfile.ZipFile) -> Tuple[str, Optional[str]]:
    """Return (SP_Flash_Tool prefix, macOS .app name) for a portable zip."""

    names = [n for n in (_safe_name(i.filename) for i in tool_zip.infolist()) if n]
    app_name: Optional[str] = None
    sp_name: Optional[str] = None
    for name in names:
        root = name.split("/", 1)[0]
        if root.lower() == "sp_flash_tool" and sp_name is None:
            sp_name = root
        if root.lower().endswith(".app") and app_name is None:
            app_name = root
    return ((sp_name + "/") if sp_name else "SP_Flash_Tool/"), app_name


def _rename_bundle(name: str, old_root: str, new_root: str) -> str:
    """Move every entry under the portable ``.app`` to its new bundle name."""

    if name == old_root:
        return new_root
    if name == old_root + "/":
        return new_root + "/"
    if name.startswith(old_root + "/"):
        return new_root + name[len(old_root) :]
    return name


def _scatter_listed_files(text: str) -> Set[str]:
    """Return the image paths a scatter declares as downloadable."""

    listed: Set[str] = set()
    current: Dict[str, str] = {}

    def flush() -> None:
        if current.get("is_download", "").lower() != "true":
            return
        raw = current.get("file_name", "").strip()
        if not raw or raw.upper() == "NONE":
            return
        name = raw.replace("\\", "/").strip().strip("\"'")
        name = posixpath.normpath(name)
        if not name or name == "." or name.startswith("/"):
            return
        if name == ".." or name.startswith("../"):
            return
        listed.add(name)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("- partition_index:"):
            flush()
            current = {}
        elif ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            current[key.strip().lstrip("- ").lower()] = value.strip().strip("\"'")
    flush()
    return listed


def _optional_package_file(rel: str) -> bool:
    """Metadata the updater reads from the package root."""

    if posixpath.dirname(rel):
        return False
    name = posixpath.basename(rel).lower()
    return (
        name in PACKAGE_IMAGE_NAMES
        or name == "manifest.json"
        or (name.endswith(".txt") and "rockbox" in name)
        or name.endswith(".auth")
    )


def _firmware_plan(
    firmware: FirmwarePackage,
    drop_preserve_txt: bool = False,
) -> Tuple[List[Tuple[str, str, bool]], List[str]]:
    """Map firmware entries to paths relative to the scatter folder.

    Only the selected scatter, the images it lists and the optional updater
    image are kept.  Other files in the firmware zip (for example a surrounding
    SP Flash Tool tree) are intentionally stripped.
    """

    plan: List[Tuple[str, str, bool]] = []
    skipped: List[str] = []
    base = firmware.scatter_dir
    base_prefix = base + "/" if base and not base.endswith("/") else base
    with zipfile.ZipFile(firmware.path) as zf:
        required: Optional[Set[str]] = None
        chosen = firmware.chosen_scatter
        if chosen:
            scatter_name = f"{base}/{chosen}" if base else chosen
            try:
                text = zf.read(scatter_name).decode("utf-8", "ignore")
            except KeyError:
                text = ""
            listed = _scatter_listed_files(text)
            required = listed or None

        keep = {name.lower() for name in (required or ())}
        if chosen:
            keep.add(chosen.lower())
        present: Set[str] = set()

        for info in zf.infolist():
            name = _safe_name(info.filename)
            if name is None:
                skipped.append(info.filename)
                continue
            if base_prefix:
                if name == base_prefix:
                    continue
                if not name.startswith(base_prefix):
                    continue
                rel = name[len(base_prefix) :]
            else:
                rel = name
            if not rel:
                continue
            is_dir = name.endswith("/")
            basename = posixpath.basename(rel.rstrip("/"))
            if not is_dir:
                present.add(rel.lower())
            if (
                drop_preserve_txt
                and not is_dir
                and basename.lower().endswith(".txt")
                and "preserve" in basename.lower()
            ):
                skipped.append(info.filename)
                continue
            if basename.lower() in ("history.ini", "fw_package.ini"):
                # Replaced by the metadata this tool writes.
                continue
            if required is not None:
                rel_lower = rel.lower()
                if is_dir:
                    if not any(item.startswith(rel_lower) for item in keep):
                        continue
                elif rel_lower not in keep and not _optional_package_file(rel_lower):
                    continue
            plan.append((info.filename, rel, is_dir))

        if required:
            missing = sorted(required - present)
            if missing:
                raise PackagingError(
                    f"{firmware.path.name} is missing scatter file(s): "
                    + ", ".join(missing)
                )
    return plan, skipped


def _sha256(path: Path) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_portable_package(
    tool_zip_path: Path,
    firmware: FirmwarePackage,
    platform: Platform,
    out_path: Path,
    ini_text: str,
    software_name: str,
    version: str,
    source_asset: str,
    log: Optional[Callable[[str], None]] = None,
    source_repo: str = "",
) -> BuildResult:
    """Write one portable package and return its metadata."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    drop_preserve_txt = drops_preserve_txt(source_repo)
    plan, skipped = _firmware_plan(firmware, drop_preserve_txt=drop_preserve_txt)
    with zipfile.ZipFile(tool_zip_path) as tool_zip:
        sp_prefix, app_name = _tool_layout(tool_zip)
        mac_layout = platform.layout == "beside_app"
        if mac_layout and not app_name:
            raise PackagingError(
                f"{tool_zip_path.name} has no .app bundle; cannot use beside_app layout"
            )

        firmware_dests: List[Tuple[str, str, bool]] = []
        seen = set()
        for src_name, rel, is_dir in plan:
            if platform.layout == "sp_flash_tool":
                dest = sp_prefix + rel
            elif mac_layout:
                dest = MAC_FIRMWARE_DIR + "/" + rel
            else:
                dest = rel
            key = dest.lower()
            if key in seen:
                continue
            seen.add(key)
            firmware_dests.append((src_name, dest, is_dir))

        extra_files: Dict[str, bytes] = {}
        if platform.layout == "sp_flash_tool":
            extra_files["fw_package.ini"] = ini_text.encode("utf-8")
            extra_files[sp_prefix + "fw_package.ini"] = ini_text.encode("utf-8")
            scatter_name = firmware.chosen_scatter or (
                firmware.scatters[0] if firmware.scatters else "scatter.txt"
            )
            if drop_preserve_txt and "preserve" in scatter_name.lower():
                scatter_name = next(
                    (n for n in firmware.scatters if "preserve" not in n.lower()),
                    "scatter.txt",
                )
            extra_files[sp_prefix + "history.ini"] = HISTORY_INI.format(
                scatter=scatter_name
            ).encode("utf-8")
        else:
            extra_files[MAC_FIRMWARE_DIR + "/fw_package.ini"] = ini_text.encode(
                "utf-8"
            )
            extra_files["README.txt"] = macos_readme(
                MAC_APP_NAME, source_asset, software_name, version
            ).encode("utf-8")
            # Renaming the bundle and retitling it invalidates the upstream code
            # signature; the copy is unsigned, so drop its stale seal too.
            try:
                plist = plistlib.loads(
                    tool_zip.read(f"{app_name}/Contents/Info.plist")
                )
            except (KeyError, ValueError):
                plist = None
            if isinstance(plist, dict):
                plist["CFBundleName"] = "Install Firmware"
                plist["CFBundleDisplayName"] = "Install Firmware"
                extra_files[
                    f"{MAC_APP_NAME}/Contents/Info.plist"
                ] = plistlib.dumps(plist)

        overrides = {name.lower() for name in extra_files}
        overrides.update(dest.lower() for _, dest, _ in firmware_dests)

        entries = 0
        with zipfile.ZipFile(
            out_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=COMPRESS_LEVEL,
            allowZip64=True,
        ) as dst:
            for info in tool_zip.infolist():
                name = _safe_name(info.filename)
                if name is None:
                    skipped.append(info.filename)
                    continue
                if mac_layout:
                    if name == "__MACOSX" or name.startswith("__MACOSX/"):
                        continue
                    if app_name:
                        name = _rename_bundle(name, app_name, MAC_APP_NAME)
                    if name.startswith(
                        f"{MAC_APP_NAME}/Contents/_CodeSignature"
                    ):
                        continue
                if name.lower() in overrides:
                    # The firmware or our metadata replaces this file.
                    continue
                compress = (
                    zipfile.ZIP_STORED
                    if (name.endswith("/") or _is_symlink(info))
                    else zipfile.ZIP_DEFLATED
                )
                _copy_info(dst, tool_zip, info, name, compress)
                entries += 1

            with zipfile.ZipFile(firmware.path) as firmware_zip:
                for src_name, dest, _is_dir in firmware_dests:
                    src_info = firmware_zip.getinfo(src_name)
                    compress = (
                        zipfile.ZIP_STORED
                        if (dest.endswith("/") or _is_symlink(src_info))
                        else zipfile.ZIP_DEFLATED
                    )
                    _copy_info(dst, firmware_zip, src_info, dest, compress)
                    entries += 1

            for name, data in sorted(extra_files.items()):
                dst.writestr(name, data)
                entries += 1

    digest, size = _sha256(out_path)
    if log:
        log(
            f"    built {out_path.name} "
            f"({size / 1024 / 1024:.1f} MiB, {entries} entries)"
        )
    return BuildResult(
        path=out_path,
        size=size,
        sha256=digest,
        source_asset=source_asset,
        platform=platform.key,
        entries=entries,
        skipped_entries=skipped,
    )
