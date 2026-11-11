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
* ``beside_dmg`` layout (macOS): the signed DMG is copied unchanged to the zip
  root and the firmware plus ``fw_package.ini`` go into lowercase ``firmware/``.

Surrounding files from the firmware zip, such as a bundled SP Flash Tool tree,
are stripped; only what the scatter declares is needed.

Everything is written with maximum deflate.  No file is buffered whole in
memory; large partition images stream through a 1 MiB window.

``repack_firmware_zip`` applies the same maximum deflate to an unchanged
original firmware zip.  When such a zip carries more than one scatter layout,
only the standard ``*_Android_scatter.txt`` files survive; layouts that stray
from that name (a ``_def``/preserve-data variant, for example) are stripped.
"""

from __future__ import annotations

import contextlib
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

from mirrorspec import (
    Platform,
    legacy_donate_txt,
    legacy_readme,
    macos_dmg_readme,
    macos_readme,
)

BUFFER_SIZE = 1024 * 1024
COMPRESS_LEVEL = 9
DRIVE_RE = re.compile(r"^[A-Za-z]:")
MAC_APP_NAME = "Install Firmware.app"
MAC_FIRMWARE_DIR = "Firmware"
MAC_DMG_NAME = "1. Install App and Firmware.dmg"
MAC_DMG_FIRMWARE_DIR = "firmware"
# macOS may add an AppleDouble metadata tree to zips it creates.  Treat both
# the normal spelling and the single-underscore variant as metadata so neither
# can leak into a generated package.
MACOS_METADATA_DIRS = frozenset({"__macosx", "_macosx"})
PACKAGE_IMAGE_NAMES = ("updater.png", "updater.jpg", "updater.jpeg")

DA_NAMES = ("MTK_AllInOne_DA.bin", "MTK_AllInOneDA.bin")

HISTORY_INI = (
    "[LastDAFilePath]\r\n"
    "lastDir={da}\r\n"
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
    da_file: Optional[str] = None

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


@dataclass
class RepackResult:
    path: Path
    size: int
    sha256: str
    entries: int
    stripped: List[str] = field(default_factory=list)


def _safe_name(raw: str) -> Optional[str]:
    """Normalise a zip entry name and reject absolute/traversal paths."""

    name = str(raw).replace("\\", "/")
    is_dir = name.endswith("/")
    if name.startswith("/") or DRIVE_RE.match(name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if any(p.lower() in MACOS_METADATA_DIRS for p in parts):
        return None
    if not parts:
        return None
    clean = "/".join(parts)
    return clean + ("/" if is_dir else "")


def _is_scatter_name(name: str) -> bool:
    """True for a ``*scatter*.txt`` file entry (any folder, any case)."""

    if name.endswith("/"):
        return False
    base = posixpath.basename(name).lower()
    return base.endswith(".txt") and "scatter" in base


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
    # A ZipInfo carries its own compression level; ZipFile.open() ignores the
    # archive's compresslevel when it is handed an existing ZipInfo.  Without
    # this every streamed entry would silently use zlib's default level 6.
    target._compresslevel = COMPRESS_LEVEL
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


def inspect_firmware(
    path: Path, source_repo: str = "", preferred_scatter: str = ""
) -> FirmwarePackage:
    """Find the scatter file and its folder inside a firmware zip.

    ``preferred_scatter`` selects a basename when a stock archive carries
    alternative hardware layouts (for example MT6572 for Y1 and MT6582 for Y2
    in the same ``rom_y2.zip``).
    """

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
    da_file = _pick_da_file(clean_names)
    if not scatters:
        return FirmwarePackage(path, "", [], None, len(clean_names), da_file)

    groups: Dict[str, List[str]] = {}
    for name in scatters:
        groups.setdefault(posixpath.dirname(name), []).append(name)

    # Prefer a scatter at the zip root; otherwise the folder with the most
    # non-preserve scatter files.
    if "" in groups:
        scatter_dir = ""
    else:
        scatter_dir = sorted(
            groups.items(), key=lambda item: (-len(item[1]), item[0].count("/"), item[0])
        )[0][0]

    basenames = [posixpath.basename(n) for n in groups[scatter_dir]]
    chosen: Optional[str] = None
    preferred = preferred_scatter.strip()
    if preferred:
        matches = [n for n in basenames if n.lower() == preferred.lower()]
        if len(matches) == 1:
            chosen = matches[0]
    android = [n for n in basenames if "ndroid_scatter" in n.lower()]
    if len(android) == 1:
        chosen = android[0]
    elif len(basenames) == 1:
        chosen = basenames[0]

    return FirmwarePackage(
        path, scatter_dir, basenames, chosen, len(clean_names), da_file
    )


def _pick_da_file(names: List[str]) -> Optional[str]:
    """Find the primary MediaTek download agent in a firmware zip."""

    candidates = [
        name
        for name in names
        if not name.endswith("/")
        and name.lower().endswith(".bin")
        and "allinone" in posixpath.basename(name).lower()
    ]
    if not candidates:
        return None
    for preferred in DA_NAMES:
        matches = [
            name
            for name in candidates
            if posixpath.basename(name).lower() == preferred.lower()
        ]
        if matches:
            return sorted(matches, key=lambda name: name.count("/"))[0]
    return sorted(candidates, key=lambda name: (name.count("/"), name))[0]


def _tool_layout(
    tool_zip: zipfile.ZipFile, normalize_legacy: bool = False
) -> Tuple[str, str, Optional[str]]:
    """Return (source prefix, destination prefix, macOS .app name).

    Legacy archives are normalised under ``SP_Flash_Tool/`` even when upstream
    publishes a flat directory (the Windows flash tool) or a versioned one
    (``SP_Flash_Tool_v5.2032_Linux/``).  The guide, history.ini and firmware
    paths then all agree.
    """

    names = [n for n in (_safe_name(i.filename) for i in tool_zip.infolist()) if n]
    app_name: Optional[str] = None
    sp_name: Optional[str] = None
    for name in names:
        root = name.split("/", 1)[0]
        if root.lower() == "sp_flash_tool" and sp_name is None:
            sp_name = root
        if root.lower().endswith(".app") and app_name is None:
            app_name = root
    if sp_name:
        prefix = sp_name + "/"
        return prefix, prefix, app_name
    if normalize_legacy:
        launchers = {"flash_tool", "flash_tool.exe", "flash_tool.sh"}
        roots = sorted({name.split("/", 1)[0] for name in names if "/" in name})
        for root in roots:
            if any(
                name.lower()
                in {(root + "/" + launcher).lower() for launcher in launchers}
                for name in names
            ):
                return root + "/", "SP_Flash_Tool/", app_name
        if any(name.lower() in launchers for name in names):
            # Flat Windows archive: everything belongs inside the named folder.
            return "", "SP_Flash_Tool/", app_name
    return "SP_Flash_Tool/", "SP_Flash_Tool/", app_name


def _tool_dest_name(name: str, source_prefix: str, dest_prefix: str) -> str:
    """Move one tool entry from its upstream root to the package root."""

    if not source_prefix:
        return dest_prefix + name
    if name == source_prefix:
        return dest_prefix
    if name.startswith(source_prefix):
        return dest_prefix + name[len(source_prefix) :]
    # Keep unrelated root files (for example a Linux udev rule that upstream
    # placed outside its versioned folder) rather than dropping them.
    return name


def _tool_da_name(tool_zip: zipfile.ZipFile, sp_prefix: str) -> str:
    """Return the DA filename already shipped inside the portable tool."""

    names = [
        n
        for n in (_safe_name(info.filename) for info in tool_zip.infolist())
        if n and not n.endswith("/") and n.startswith(sp_prefix)
    ]
    for preferred in DA_NAMES:
        matches = [n for n in names if n.lower() == (sp_prefix + preferred).lower()]
        if matches:
            return posixpath.basename(matches[0])

    # Some Updater CE builds may rename the DA.  Their own history.ini names
    # the file they consider the default, so trust it when it is present.
    try:
        history = tool_zip.read(sp_prefix + "history.ini").decode(
            "utf-8", "ignore"
        )
    except KeyError:
        history = ""
    for line in history.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() != "lastdir":
            continue
        name = posixpath.basename(value.strip().strip('"\'').replace("\\", "/"))
        if name and any(
            n.lower() == (sp_prefix + name).lower() for n in names
        ):
            return name

    candidates = [
        posixpath.basename(n)
        for n in names
        if posixpath.basename(n).lower().endswith(".bin")
        and "allinone" in posixpath.basename(n).lower()
    ]
    if candidates:
        return sorted(set(candidates), key=str.lower)[0]
    # This is only a fallback for a malformed/unusual portable app.  The DA
    # path in history.ini must remain a bare filename, never an absolute path.
    return DA_NAMES[0]


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
    """Metadata the updater reads from the package root or metadata/ folder."""

    parent = posixpath.dirname(rel).lower()
    name = posixpath.basename(rel).lower()
    if parent == "metadata":
        return name.endswith(".json")
    if parent:
        return False
    return (
        name in PACKAGE_IMAGE_NAMES
        or name == "manifest.json"
        or (name.endswith(".txt") and "rockbox" in name)
        or name.endswith(".auth")
    )


def _firmware_plan(
    firmware: FirmwarePackage,
) -> Tuple[List[Tuple[str, str, bool]], List[str]]:
    """Map firmware entries to paths relative to the scatter folder.

    Only the selected scatter, the images it lists and the optional updater
    image are kept.  Other files in the firmware zip (for example a surrounding
    SP Flash Tool tree) are intentionally stripped, and no other scatter file
    is ever carried into the package.
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

        # Secure-boot stock archives ship signed images (``boot-sign.img``)
        # while the scatter keeps the logical name (``boot.img``).  Copy the
        # signed bytes under the name the scatter declares so SP Flash Tool
        # and Updater CE find them.
        available: Set[str] = set()
        for info in zf.infolist():
            name = _safe_name(info.filename)
            if name is None or name.endswith("/"):
                continue
            if base_prefix:
                if not name.startswith(base_prefix):
                    continue
                available.add(name[len(base_prefix) :].lower())
            else:
                available.add(name.lower())
        alias_targets: Dict[str, str] = {}
        for name in required or ():
            key = name.lower()
            if key in available:
                continue
            stem, ext = posixpath.splitext(name)
            signed = f"{stem}-sign{ext}".lower()
            if signed in available:
                alias_targets[signed] = name

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
            # A portable package carries exactly one scatter: the one the
            # original firmware zip declares.  Preserve-data and alternate
            # scatter files are never copied, whatever the source repo.
            if (
                not is_dir
                and basename.lower().endswith(".txt")
                and "scatter" in basename.lower()
                and (not chosen or basename.lower() != chosen.lower())
            ):
                skipped.append(info.filename)
                continue
            if basename.lower() in ("history.ini", "fw_package.ini"):
                # Replaced by the metadata this tool writes.
                continue
            if (
                not is_dir
                and firmware.da_file
                and name.lower() == firmware.da_file.lower()
            ):
                # The primary DA is copied to the SP Flash Tool root below.
                skipped.append(info.filename)
                continue
            if required is not None:
                rel_lower = rel.lower()
                if is_dir:
                    if not any(item.startswith(rel_lower) for item in keep):
                        continue
                elif rel_lower not in keep and not _optional_package_file(rel_lower):
                    alias_target = alias_targets.get(rel_lower)
                    if alias_target:
                        present.add(alias_target.lower())
                        plan.append((info.filename, alias_target, False))
                    continue
            plan.append((info.filename, rel, is_dir))

        if required:
            missing = sorted(
                name for name in required if name.lower() not in present
            )
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


def _is_standard_scatter(name: str) -> bool:
    """True for the MediaTek ``M*****_Android_scatter.txt`` spelling."""

    return posixpath.basename(name).lower().endswith("android_scatter.txt")


def _original_scatter_keep(names: List[str]) -> Set[str]:
    """Choose which scatter entries an untouched original zip keeps.

    A stock zip with a single scatter is left alone.  When several layouts are
    present, keep the standard ``*_Android_scatter.txt`` files and strip the
    variants that stray from that name.  If none of them uses the standard
    name, keep every scatter rather than guessing.
    """

    scatters = [name for name in names if _is_scatter_name(name)]
    if len(scatters) < 2:
        return set(scatters)
    standard = [name for name in scatters if _is_standard_scatter(name)]
    return set(standard or scatters)


def repack_firmware_zip(
    source: Path, out_path: Path, log: Optional[Callable[[str], None]] = None
) -> RepackResult:
    """Rewrite an original firmware zip with maximum deflate.

    Entry names, timestamps, permissions and symlinks are preserved so the
    archive still behaves like the source.  Only alternate scatter files are
    dropped, and only when the zip carries more than one scatter layout and at
    least one uses the standard ``*_Android_scatter.txt`` name.
    """

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    if source.resolve() == out_path.resolve():
        raise PackagingError(f"{source.name} would overwrite itself")

    try:
        with zipfile.ZipFile(source) as src:
            infos = src.infolist()
            keep = _original_scatter_keep([info.filename for info in infos])
            stripped = [
                info.filename
                for info in infos
                if _is_scatter_name(info.filename)
                and info.filename not in keep
            ]
            entries = 0
            with zipfile.ZipFile(
                out_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=COMPRESS_LEVEL,
                allowZip64=True,
            ) as dst:
                for info in infos:
                    if info.filename in stripped:
                        continue
                    target = zipfile.ZipInfo(
                        info.filename, _safe_date_time(info)
                    )
                    target.external_attr = info.external_attr
                    target.internal_attr = info.internal_attr
                    target.create_system = info.create_system
                    target.comment = info.comment
                    target.compress_type = (
                        zipfile.ZIP_STORED
                        if info.filename.endswith("/") or _is_symlink(info)
                        else zipfile.ZIP_DEFLATED
                    )
                    target._compresslevel = COMPRESS_LEVEL
                    target.file_size = info.file_size
                    if info.filename.endswith("/") or _is_symlink(info):
                        dst.writestr(target, src.read(info))
                    else:
                        with src.open(info) as reader, dst.open(
                            target,
                            "w",
                            force_zip64=target.file_size * 1.05
                            > zipfile.ZIP64_LIMIT,
                        ) as writer:
                            shutil.copyfileobj(reader, writer, BUFFER_SIZE)
                    entries += 1
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise PackagingError(f"{source.name} is not a readable zip: {exc}") from exc

    digest, size = _sha256(out_path)
    before = source.stat().st_size
    if log:
        log(
            f"    repacked {source.name} "
            f"({before / 1024 / 1024:.1f} -> {size / 1024 / 1024:.1f} MiB, "
            f"{entries} entries)"
        )
    return RepackResult(
        path=out_path,
        size=size,
        sha256=digest,
        entries=entries,
        stripped=stripped,
    )


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
    post_install_name: str = "",
    post_install_data: bytes = b"",
    target_model: str = "",
    target_type: str = "",
    driver_path: Optional[Path] = None,
) -> BuildResult:
    """Write one portable package and return its metadata."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    if firmware.scatters and not firmware.chosen_scatter:
        raise PackagingError(
            f"{firmware.path.name} has {len(firmware.scatters)} scatter files; "
            "a portable package must carry exactly one"
        )
    plan, skipped = _firmware_plan(firmware)
    dmg_layout = platform.layout == "beside_dmg"
    if dmg_layout and tool_zip_path.suffix.lower() != ".dmg":
        raise PackagingError(
            f"{tool_zip_path.name} is not a DMG; cannot use beside_dmg layout"
        )
    tool_context = (
        contextlib.nullcontext(None)
        if dmg_layout
        else zipfile.ZipFile(tool_zip_path)
    )
    with tool_context as tool_zip:
        if dmg_layout:
            source_prefix = dest_prefix = ""
            app_name = None
        else:
            source_prefix, dest_prefix, app_name = _tool_layout(
                tool_zip, normalize_legacy=platform.legacy
            )
        mac_layout = platform.layout == "beside_app"
        if mac_layout and not app_name:
            raise PackagingError(
                f"{tool_zip_path.name} has no .app bundle; cannot use beside_app layout"
            )

        firmware_dests: List[Tuple[str, str, bool]] = []
        seen = set()
        for src_name, rel, is_dir in plan:
            if platform.layout == "sp_flash_tool":
                dest = dest_prefix + rel
            elif mac_layout:
                dest = MAC_FIRMWARE_DIR + "/" + rel
            elif dmg_layout:
                dest = MAC_DMG_FIRMWARE_DIR + "/" + rel
            else:
                dest = rel
            key = dest.lower()
            if key in seen:
                continue
            seen.add(key)
            firmware_dests.append((src_name, dest, is_dir))

        extra_files: Dict[str, bytes] = {}
        if platform.layout == "sp_flash_tool":
            da_name = _tool_da_name(tool_zip, source_prefix)
            if firmware.da_file:
                # A firmware-specific DA is more authoritative than the generic
                # one bundled by Updater CE.  Put it at the tool root and keep
                # only its bare filename in history.ini.
                da_name = posixpath.basename(firmware.da_file)
                da_dest = dest_prefix + da_name
                if da_dest.lower() not in seen:
                    firmware_dests.append((firmware.da_file, da_dest, False))
                    seen.add(da_dest.lower())
            extra_files["fw_package.ini"] = ini_text.encode("utf-8")
            extra_files[dest_prefix + "fw_package.ini"] = ini_text.encode("utf-8")
            scatter_name = firmware.chosen_scatter or "scatter.txt"
            extra_files[dest_prefix + "history.ini"] = HISTORY_INI.format(
                scatter=scatter_name, da=da_name
            ).encode("utf-8")
            if platform.legacy:
                # Old Windows and 32-bit Linux cannot run Updater CE, so the
                # package explains the tool beside the firmware, follows the
                # driver-install/reboot/Download/connect flow and shows where
                # the projects can be supported.  The note and guide stay in
                # the extraction root and beside the tool, because a reader who
                # unzips with Explorer often only sees one of the two.
                donate = legacy_donate_txt(
                    software_name, version, source_asset
                ).encode("utf-8")
                extra_files["donate.txt"] = donate
                extra_files[dest_prefix + "donate.txt"] = donate
                guide = legacy_readme(
                    software_name,
                    version,
                    source_asset,
                    scatter_name,
                    platform_key=platform.key,
                    model=target_model,
                    target_type=target_type,
                ).encode("utf-8")
                extra_files["README.txt"] = guide
                extra_files[dest_prefix + "README.txt"] = guide
                if driver_path is not None:
                    driver_name = _safe_name(driver_path.name)
                    if not driver_name or "/" in driver_name:
                        raise PackagingError(
                            f"unsafe driver filename: {driver_path.name!r}"
                        )
                    extra_files["Driver/" + driver_name] = driver_path.read_bytes()
        elif dmg_layout:
            extra_files[MAC_DMG_FIRMWARE_DIR + "/fw_package.ini"] = ini_text.encode(
                "utf-8"
            )
            extra_files["README.txt"] = macos_dmg_readme(
                MAC_DMG_NAME,
                MAC_DMG_FIRMWARE_DIR,
                source_asset,
                software_name,
                version,
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

        if post_install_name and post_install_data:
            clean_post = _safe_name(post_install_name)
            if (
                not clean_post
                or clean_post.endswith("/")
                or "/" in clean_post
            ):
                raise PackagingError(
                    f"unsafe post-install filename: {post_install_name!r}"
                )
            extra_files[clean_post] = post_install_data

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
            if dmg_layout:
                # The extracted DMG stays byte-for-byte identical; deflating
                # the zip member only shrinks its transport size.
                dst.write(
                    tool_zip_path,
                    arcname=MAC_DMG_NAME,
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=COMPRESS_LEVEL,
                )
                entries += 1
            else:
                for info in tool_zip.infolist():
                    name = _safe_name(info.filename)
                    if name is None:
                        skipped.append(info.filename)
                        continue
                    if mac_layout:
                        if app_name:
                            name = _rename_bundle(name, app_name, MAC_APP_NAME)
                        if name.startswith(
                            f"{MAC_APP_NAME}/Contents/_CodeSignature"
                        ):
                            continue
                    if platform.layout == "sp_flash_tool":
                        name = _tool_dest_name(name, source_prefix, dest_prefix)
                    if name.lower() in overrides:
                        # The firmware or our metadata replaces this file.
                        continue
                    if _is_scatter_name(name):
                        # The portable app carries reference scatters of its
                        # own (``_internal/assets/compat``).  The package's
                        # only scatter must be the firmware's.
                        skipped.append(info.filename)
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
