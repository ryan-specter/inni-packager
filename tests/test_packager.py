import os
import plistlib
import random
import sys
import unittest
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402
from packager import (  # noqa: E402
    PackagingError,
    build_portable_package,
    inspect_firmware,
    repack_firmware_zip,
)

DIR_MODE = (0o40755 << 16) | 0x10
FILE_MODE = 0o100644 << 16
LINK_MODE = 0o120755 << 16


def write_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode
            if name.endswith("/"):
                info.external_attr = DIR_MODE
            archive.writestr(info, data)


def names_of(path):
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def deflate_payload():
    """Bytes whose level 6 and level 9 deflate sizes differ."""

    words = [
        b"system",
        b"boot",
        b"vendor",
        b"recovery",
        b"partition",
        b"scatter",
        b"android",
        b"firmware",
        b"flash",
        b"updater",
        b"image",
        b"userdata",
        b"cache",
        b"metadata",
        b"preloader",
        b"logo",
        b"lk",
        b"bootimg",
        b"sparse",
        b"chunk",
    ]
    rng = random.Random(20261106)
    return b" ".join(rng.choice(words) for _ in range(60000)) + bytes(
        rng.randrange(256) for _ in range(20000)
    )


def raw_deflate(data, level=9):
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


class PackagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / ".tmp-packager"
        self.tmp.mkdir(exist_ok=True)
        self.cfg = mirrorspec.load_config(str(ROOT / "mirror.config.json"))

        self.firmware = self.tmp / "rom.zip"
        payload = os.urandom(2048)
        write_zip(
            self.firmware,
            [
                ("MT6572_Android_scatter.txt", b"partition_index: SYS0\n", FILE_MODE),
                ("preserve_data_scatter.txt", b"preserve\n", FILE_MODE),
                ("boot.img", payload, FILE_MODE),
                ("images/", b"", DIR_MODE),
                ("images/system.img", payload, FILE_MODE),
                ("history.ini", b"[stale]\n", FILE_MODE),
                ("__MACOSX/._boot.img", b"junk", FILE_MODE),
                ("_MACOSX/._system.img", b"junk", FILE_MODE),
            ],
        )

        self.windows_tool = self.tmp / "windows.zip"
        write_zip(
            self.windows_tool,
            [
                ("SP_Flash_Tool/", b"", DIR_MODE),
                ("SP_Flash_Tool/flash_tool.exe", b"tool", FILE_MODE),
                ("SP_Flash_Tool/MTK_AllInOne_DA.bin", b"da", FILE_MODE),
                ("SP_Flash_Tool/history.ini", b"[old]\n", FILE_MODE),
                ("InnioasisUpdater", b"python", FILE_MODE),
                ("_internal/base_library.zip", b"zip", FILE_MODE),
                ("_internal/assets/compat/MT6572_Android_scatter.txt", b"ref", FILE_MODE),
                ("_internal/assets/compat/MT6582_Android_scatter.txt", b"ref", FILE_MODE),
                ("_internal/assets/compat/libpng12.so.0", b"lib", FILE_MODE),
                ("SP_Flash_Tool/dl_without_scatter.xml", b"<xml/>", FILE_MODE),
                ("__MACOSX/._InnioasisUpdater", b"junk", FILE_MODE),
                ("_MACOSX/._flash_tool.exe", b"junk", FILE_MODE),
            ],
        )

        self.mac_tool = self.tmp / "mac.zip"
        write_zip(
            self.mac_tool,
            [
                ("Innioasis Updater.app/", b"", DIR_MODE),
                ("Innioasis Updater.app/Contents/Info.plist", plistlib.dumps({
                    "CFBundleName": "Innioasis Updater",
                    "CFBundleDisplayName": "Innioasis Updater",
                    "CFBundleExecutable": "Innioasis Updater",
                }), FILE_MODE),
                ("Innioasis Updater.app/Contents/_CodeSignature/CodeResources", b"sig", FILE_MODE),
                ("Innioasis Updater.app/Contents/MacOS/Innioasis Updater", b"binary", FILE_MODE),
                ("Innioasis Updater.app/Contents/Frameworks/QtCore", b"QtCore.framework/Versions/A/QtCore", LINK_MODE),
                ("__MACOSX/._Innioasis Updater.app", b"junk", FILE_MODE),
                ("_MACOSX/._Innioasis Updater.app", b"junk", FILE_MODE),
            ],
        )

        self.mac_app_platform = replace(
            self.cfg.platform("macos"),
            layout="beside_app",
            updater_asset="Innioasis.Updater.for.Mac.zip",
        )

        self.ini = mirrorspec.fw_package_ini(
            "Original Software", "3.1.2", "Y1", "Type A"
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_no_macos_metadata(self, names):
        metadata = {"__macosx", "_macosx"}
        for name in names:
            for part in name.split("/"):
                self.assertNotIn(part.lower(), metadata, name)

    def test_inspect_picks_non_preserve_scatter(self):
        package = inspect_firmware(self.firmware)
        self.assertEqual(package.scatter_dir, "")
        self.assertEqual(package.chosen_scatter, "MT6572_Android_scatter.txt")
        self.assertEqual(package.scatters, ["MT6572_Android_scatter.txt"])

    def test_only_the_source_scatter_is_packed(self):
        # Whatever the source repo, the package carries the firmware's own
        # scatter and never an alternative (preserve-data) layout.
        for repo in ("schulzcode/y2linux", "schulzcode/Y2Reborn", "schulzcode/other"):
            package = inspect_firmware(self.firmware, source_repo=repo)
            self.assertEqual(package.scatters, ["MT6572_Android_scatter.txt"])
            out = self.tmp / "rom-windows.zip"
            build_portable_package(
                self.windows_tool,
                package,
                self.cfg.platform("windows"),
                out,
                self.ini,
                "Original Software",
                "3.1.2",
                "rom.zip",
                source_repo=repo,
            )
            names = names_of(out)
            self.assertEqual(
                [n for n in names if n.lower().endswith("scatter.txt")],
                ["SP_Flash_Tool/MT6572_Android_scatter.txt"],
            )
            self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
            with zipfile.ZipFile(out) as archive:
                history = archive.read("SP_Flash_Tool/history.ini").decode("utf-8")
            self.assertIn("scatterHistory=MT6572_Android_scatter.txt", history)

        mac_out = self.tmp / "rom-macos.zip"
        build_portable_package(
            self.mac_tool,
            inspect_firmware(self.firmware, source_repo="schulzcode/other"),
            self.mac_app_platform,
            mac_out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
            source_repo="schulzcode/other",
        )
        mac_names = names_of(mac_out)
        self.assertNotIn("Firmware/preserve_data_scatter.txt", mac_names)
        self.assertEqual(
            [n for n in mac_names if n.lower().endswith("scatter.txt")],
            ["Firmware/MT6572_Android_scatter.txt"],
        )

    def test_legacy_package_carries_donate_note_and_flash_tool_only(self):
        # Windows XP/7/8 users get the flash tool on its own, not the Updater CE
        # app, plus the note that says how the projects can be supported.
        tool_only = self.tmp / "flash-tool-only.zip"
        write_zip(
            tool_only,
            [
                ("SP_Flash_Tool/", b"", DIR_MODE),
                ("SP_Flash_Tool/flash_tool.exe", b"tool", FILE_MODE),
                ("SP_Flash_Tool/MTK_AllInOne_DA.bin", b"da", FILE_MODE),
                ("SP_Flash_Tool/history.ini", b"[old]\n", FILE_MODE),
            ],
        )
        out = self.tmp / "rom-windows-legacy.zip"
        result = build_portable_package(
            tool_only,
            inspect_firmware(self.firmware),
            self.cfg.platform("windows-legacy"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
        )
        self.assertEqual(result.platform, "windows-legacy")
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/flash_tool.exe", names)
        self.assertIn("donate.txt", names)
        self.assertIn("SP_Flash_Tool/donate.txt", names)
        self.assertIn("README.txt", names)
        self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
        self.assertNotIn("InnioasisUpdater", names)
        with zipfile.ZipFile(out) as archive:
            donate = archive.read("donate.txt").decode("utf-8")
            readme = archive.read("README.txt").decode("utf-8")
        for _label, url in mirrorspec.DONATION_LINKS:
            self.assertIn(url, donate)
        self.assertIn("Install the driver first", readme)
        self.assertIn("Reboot the PC", readme)
        self.assertIn("flash_tool.exe", readme)
        self.assertIn("legacy-windows.html", readme)

    def test_flat_windows_legacy_tool_is_normalized_and_driver_is_packed(self):
        # Upstream flash_tool_windows.zip is flat; the package must still put
        # the tool, firmware and driver under the folder the guide names.
        tool_only = self.tmp / "flat-flash-tool.zip"
        write_zip(
            tool_only,
            [
                ("flash_tool.exe", b"tool", FILE_MODE),
                ("MTK_AllInOne_DA.bin", b"da", FILE_MODE),
                ("Driver/Download.url", b"driver link", FILE_MODE),
                ("history.ini", b"[old]\n", FILE_MODE),
            ],
        )
        driver = self.tmp / "1.Install.Driver.First.and.Reboot.PC.exe"
        driver.write_bytes(b"driver installer")
        out = self.tmp / "flat-windows-legacy.zip"
        build_portable_package(
            tool_only,
            inspect_firmware(self.firmware),
            self.cfg.platform("windows-legacy"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
            driver_path=driver,
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/flash_tool.exe", names)
        self.assertIn("SP_Flash_Tool/MTK_AllInOne_DA.bin", names)
        self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
        self.assertIn(
            "Driver/1.Install.Driver.First.and.Reboot.PC.exe", names
        )
        self.assertNotIn("flash_tool.exe", names)
        with zipfile.ZipFile(out) as archive:
            readme = archive.read("README.txt").decode("utf-8")
        self.assertIn("Driver folder", readme)
        self.assertIn("SP_Flash_Tool", readme)

    def test_versioned_linux_legacy_tool_is_normalized(self):
        tool_only = self.tmp / "versioned-flash-tool.zip"
        write_zip(
            tool_only,
            [
                ("SP_Flash_Tool_v5.2032_Linux/", b"", DIR_MODE),
                ("SP_Flash_Tool_v5.2032_Linux/flash_tool", b"tool", FILE_MODE),
                ("SP_Flash_Tool_v5.2032_Linux/MTK_AllInOne_DA.bin", b"da", FILE_MODE),
                ("SP_Flash_Tool_v5.2032_Linux/99-ttyacms.rules", b"rules", FILE_MODE),
            ],
        )
        out = self.tmp / "versioned-linux-legacy.zip"
        build_portable_package(
            tool_only,
            inspect_firmware(self.firmware),
            self.cfg.platform("linux-legacy"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/flash_tool", names)
        self.assertIn("SP_Flash_Tool/99-ttyacms.rules", names)
        self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
        self.assertFalse(
            any(name.startswith("SP_Flash_Tool_v5.2032_Linux/") for name in names)
        )

    def test_linux_legacy_package_uses_the_32_bit_tool_guide(self):
        # 32-bit Linux cannot run the AppImage, so the same flash-tool-only
        # archive is packed beside the firmware and the guide names the 32-bit
        # runtime libraries the tool needs.
        tool_only = self.tmp / "flash-tool-linux.zip"
        write_zip(
            tool_only,
            [
                ("SP_Flash_Tool/", b"", DIR_MODE),
                ("SP_Flash_Tool/flash_tool", b"tool", FILE_MODE),
                ("SP_Flash_Tool/MTK_AllInOne_DA.bin", b"da", FILE_MODE),
                ("Drivers/", b"", DIR_MODE),
                ("Drivers/install.sh", b"#!/bin/sh\n", FILE_MODE),
            ],
        )
        out = self.tmp / "rom-linux-legacy.zip"
        result = build_portable_package(
            tool_only,
            inspect_firmware(self.firmware),
            self.cfg.platform("linux-legacy"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
            target_model="Y1",
            target_type="Type A",
        )
        self.assertEqual(result.platform, "linux-legacy")
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/flash_tool", names)
        self.assertIn("SP_Flash_Tool/README.txt", names)
        self.assertIn("Drivers/install.sh", names)
        with zipfile.ZipFile(out) as archive:
            readme = archive.read("README.txt").decode("utf-8")
        self.assertIn("./flash_tool", readme)
        self.assertIn("32-bit", readme)
        self.assertIn("Y1 Type A", readme)
        self.assertIn("legacy-linux-32.html", readme)

    def test_windows_package_has_no_donate_note(self):
        out = self.tmp / "rom-windows.zip"
        build_portable_package(
            self.windows_tool,
            inspect_firmware(self.firmware),
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
        )
        self.assertNotIn("donate.txt", names_of(out))

    def test_multiple_scatter_files_are_refused(self):
        multi = self.tmp / "multi.zip"
        write_zip(
            multi,
            [
                ("MT6572_Android_scatter.txt", b"scatter", FILE_MODE),
                ("MT6582_Android_scatter.txt", b"scatter", FILE_MODE),
                ("boot.img", b"boot", FILE_MODE),
            ],
        )
        package = inspect_firmware(multi)
        self.assertIsNone(package.chosen_scatter)
        with self.assertRaises(PackagingError):
            build_portable_package(
                self.windows_tool,
                package,
                self.cfg.platform("windows"),
                self.tmp / "multi-windows.zip",
                self.ini,
                "Original Software",
                "1",
                "multi.zip",
            )

    def test_signed_images_are_packed_under_scatter_names(self):
        signed = self.tmp / "rom-signed.zip"
        scatter = (
            "- partition_index: SYS0\n"
            "  partition_name: boot\n"
            "  file_name: boot.img\n"
            "  is_download: true\n"
            "- partition_index: SYS1\n"
            "  partition_name: system\n"
            "  file_name: system.img\n"
            "  is_download: true\n"
        ).encode("utf-8")
        write_zip(
            signed,
            [
                ("MT6753_Android_scatter.txt", scatter, FILE_MODE),
                ("boot-sign.img", b"signed boot", FILE_MODE),
                ("system-sign.img", b"signed system", FILE_MODE),
            ],
        )
        package = inspect_firmware(signed)
        out = self.tmp / "signed-windows.zip"
        build_portable_package(
            self.windows_tool,
            package,
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "1.0",
            "rom-signed.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/boot.img", names)
        self.assertIn("SP_Flash_Tool/system.img", names)
        self.assertNotIn("SP_Flash_Tool/boot-sign.img", names)
        self.assertNotIn("SP_Flash_Tool/system-sign.img", names)

    def test_verified_images_are_packed_under_scatter_names(self):
        verified = self.tmp / "rom-verified.zip"
        scatter = (
            "- partition_index: SYS0\n"
            "  partition_name: lk\n"
            "  file_name: lk.img\n"
            "  is_download: true\n"
            "- partition_index: SYS1\n"
            "  partition_name: logo\n"
            "  file_name: logo.bin\n"
            "  is_download: true\n"
        ).encode("utf-8")
        write_zip(
            verified,
            [
                ("MT6765_Android_scatter.txt", scatter, FILE_MODE),
                ("lk-verified.img", b"verified lk", FILE_MODE),
                ("logo-verified.bin", b"verified logo", FILE_MODE),
            ],
        )
        package = inspect_firmware(verified)
        out = self.tmp / "verified-windows.zip"
        build_portable_package(
            self.windows_tool,
            package,
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "1.0",
            "rom-verified.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/lk.img", names)
        self.assertIn("SP_Flash_Tool/logo.bin", names)
        self.assertNotIn("SP_Flash_Tool/lk-verified.img", names)
        self.assertNotIn("SP_Flash_Tool/logo-verified.bin", names)

    def test_omitted_data_image_is_disabled_in_the_packed_scatter(self):
        source = self.tmp / "rom-with-userdata.zip"
        scatter = (
            "- partition_index: SYS0\n"
            "  partition_name: userdata\n"
            "  file_name: userdata.img\n"
            "  is_download: true\n"
            "- partition_index: SYS1\n"
            "  partition_name: boot\n"
            "  file_name: boot.img\n"
            "  is_download: true\n"
        ).encode("utf-8")
        write_zip(
            source,
            [
                ("MT6572_Android_scatter.txt", scatter, FILE_MODE),
                ("userdata.img", b"user data", FILE_MODE),
                ("boot.img", b"boot", FILE_MODE),
            ],
        )
        out = self.tmp / "omitted-windows.zip"
        build_portable_package(
            self.windows_tool,
            inspect_firmware(source),
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "1.0",
            "rom-with-userdata.zip",
            omit_images=["userdata.img"],
        )
        names = names_of(out)
        self.assertNotIn("SP_Flash_Tool/userdata.img", names)
        self.assertIn("SP_Flash_Tool/boot.img", names)
        with zipfile.ZipFile(out) as archive:
            packed_scatter = archive.read(
                "SP_Flash_Tool/MT6572_Android_scatter.txt"
            ).decode("utf-8")
        self.assertIn("file_name: NONE", packed_scatter)
        self.assertIn("is_download: false", packed_scatter)
        self.assertIn("file_name: boot.img", packed_scatter)

    def test_preferred_scatter_chooses_a_hardware_layout(self):
        multi = self.tmp / "dual.zip"
        write_zip(
            multi,
            [
                ("MT6572_Android_scatter.txt", b"y1 layout", FILE_MODE),
                ("MT6582_Android_scatter.txt", b"y2 layout", FILE_MODE),
                ("boot.img", b"boot", FILE_MODE),
            ],
        )
        package = inspect_firmware(multi, preferred_scatter="MT6582_Android_scatter.txt")
        self.assertEqual(package.chosen_scatter, "MT6582_Android_scatter.txt")
        out = self.tmp / "dual-windows.zip"
        build_portable_package(
            self.windows_tool,
            package,
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "1.0",
            "dual.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/MT6582_Android_scatter.txt", names)
        self.assertNotIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
        with zipfile.ZipFile(out) as archive:
            history = archive.read("SP_Flash_Tool/history.ini").decode("utf-8")
        self.assertIn("scatterHistory=MT6582_Android_scatter.txt", history)

    def test_only_scatter_listed_files_and_icon_are_packed(self):
        pruned = self.tmp / "pruned.zip"
        scatter = (
            "- partition_index: SYS0\n"
            "  partition_name: boot\n"
            "  file_name: boot.img\n"
            "  is_download: true\n"
            "- partition_index: SYS1\n"
            "  partition_name: system\n"
            "  file_name: images/system.img\n"
            "  is_download: true\n"
            "- partition_index: SYS2\n"
            "  partition_name: MBR\n"
            "  file_name: MBR\n"
            "  is_download: true\n"
            "- partition_index: SYS3\n"
            "  partition_name: EBR1\n"
            "  file_name: EBR1\n"
            "  is_download: true\n"
        ).encode("utf-8")
        write_zip(
            pruned,
            [
                ("MT6572_Android_scatter.txt", scatter, FILE_MODE),
                ("boot.img", b"boot", FILE_MODE),
                ("images/", b"", DIR_MODE),
                ("images/system.img", b"system", FILE_MODE),
                ("images/unlisted.bin", b"extra", FILE_MODE),
                ("MBR", b"mbr", FILE_MODE),
                ("EBR1", b"ebr", FILE_MODE),
                ("SP_Flash_Tool/leftover.exe", b"tool", FILE_MODE),
                ("updater.png", b"icon", FILE_MODE),
                ("rockbox_themes.txt", b"marker", FILE_MODE),
                ("auth.auth", b"auth", FILE_MODE),
                ("manifest.json", b"{}", FILE_MODE),
                ("fw_package.ini", b"[old]\n", FILE_MODE),
            ],
        )
        package = inspect_firmware(pruned)
        self.assertEqual(package.chosen_scatter, "MT6572_Android_scatter.txt")

        out = self.tmp / "pruned-windows.zip"
        build_portable_package(
            self.windows_tool,
            package,
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "pruned.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
        self.assertIn("SP_Flash_Tool/boot.img", names)
        self.assertIn("SP_Flash_Tool/images/", names)
        self.assertIn("SP_Flash_Tool/images/system.img", names)
        self.assertIn("SP_Flash_Tool/MBR", names)
        self.assertIn("SP_Flash_Tool/EBR1", names)
        self.assertIn("SP_Flash_Tool/updater.png", names)
        self.assertIn("SP_Flash_Tool/rockbox_themes.txt", names)
        self.assertIn("SP_Flash_Tool/auth.auth", names)
        self.assertIn("SP_Flash_Tool/manifest.json", names)
        self.assertNotIn("SP_Flash_Tool/images/unlisted.bin", names)
        self.assertNotIn("SP_Flash_Tool/leftover.exe", names)

        mac_out = self.tmp / "pruned-macos.zip"
        build_portable_package(
            self.mac_tool,
            package,
            self.mac_app_platform,
            mac_out,
            self.ini,
            "Original Software",
            "3.1.2",
            "pruned.zip",
        )
        mac_names = names_of(mac_out)
        self.assertIn("Firmware/MT6572_Android_scatter.txt", mac_names)
        self.assertIn("Firmware/boot.img", mac_names)
        self.assertIn("Firmware/images/system.img", mac_names)
        self.assertIn("Firmware/MBR", mac_names)
        self.assertIn("Firmware/EBR1", mac_names)
        self.assertIn("Firmware/updater.png", mac_names)
        self.assertIn("Firmware/rockbox_themes.txt", mac_names)
        self.assertIn("Firmware/auth.auth", mac_names)
        self.assertIn("Firmware/manifest.json", mac_names)
        self.assertNotIn("Firmware/images/unlisted.bin", mac_names)
        self.assertNotIn("Firmware/leftover.exe", mac_names)

    def test_windows_layout(self):
        out = self.tmp / "rom-windows.zip"
        platform = self.cfg.platform("windows")
        result = build_portable_package(
            self.windows_tool,
            inspect_firmware(self.firmware),
            platform,
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/boot.img", names)
        self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
        self.assertIn("SP_Flash_Tool/images/system.img", names)
        self.assertIn("SP_Flash_Tool/images/", names)
        self.assertIn("SP_Flash_Tool/fw_package.ini", names)
        self.assertIn("fw_package.ini", names)
        self.assertIn("SP_Flash_Tool/flash_tool.exe", names)
        self.assertIn("SP_Flash_Tool/MTK_AllInOne_DA.bin", names)
        self.assertEqual(names.count("SP_Flash_Tool/history.ini"), 1)
        self.assert_no_macos_metadata(names)
        # The portable app's own reference scatters never ship; the firmware's
        # scatter is the only one in the whole package.  Other app files stay.
        self.assertEqual(
            [n for n in names if n.lower().endswith(".txt") and "scatter" in n.lower()],
            ["SP_Flash_Tool/MT6572_Android_scatter.txt"],
        )
        self.assertIn("_internal/assets/compat/libpng12.so.0", names)
        self.assertIn("SP_Flash_Tool/dl_without_scatter.xml", names)
        self.assertIn("_internal/base_library.zip", names)

        with zipfile.ZipFile(out) as archive:
            history = archive.read("SP_Flash_Tool/history.ini").decode("utf-8")
            self.assertIn("scatterHistory=MT6572_Android_scatter.txt", history)
            self.assertIn("lastDir=MTK_AllInOne_DA.bin", history)
            self.assertNotIn("[old]", history)
            self.assertNotIn("C:\\", history)
            self.assertNotIn("/", history.replace("[LastDAFilePath]", "").replace("[RecentOpenFile]", "").replace("@Invalid()", ""))
            self.assertNotIn("\\", history.replace("[LastDAFilePath]", "").replace("[RecentOpenFile]", "").replace("@Invalid()", ""))

    def test_macos_metadata_is_not_packed_for_windows_or_linux(self):
        for key in ("windows", "linux"):
            out = self.tmp / f"metadata-{key}.zip"
            build_portable_package(
                self.windows_tool,
                inspect_firmware(self.firmware),
                self.cfg.platform(key),
                out,
                self.ini,
                "Original Software",
                "3.1.2",
                "rom.zip",
            )
            self.assert_no_macos_metadata(names_of(out))

    def test_firmware_da_is_copied_to_the_tool_root_without_a_path(self):
        source = self.tmp / "with-da.zip"
        write_zip(
            source,
            [
                ("MT6572_Android_scatter.txt", b"partition_index: SYS0\n", FILE_MODE),
                ("boot.img", b"boot", FILE_MODE),
                ("pkg/MTK_AllInOneDA.bin", b"firmware-da", FILE_MODE),
            ],
        )
        package = inspect_firmware(source)
        self.assertEqual(package.da_file, "pkg/MTK_AllInOneDA.bin")
        out = self.tmp / "with-da-windows.zip"
        result = build_portable_package(
            self.windows_tool,
            package,
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "with-da.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/MTK_AllInOneDA.bin", names)
        self.assertNotIn("SP_Flash_Tool/pkg/MTK_AllInOneDA.bin", names)
        self.assertNotIn("SP_Flash_Tool/pkg/", names)
        with zipfile.ZipFile(out) as archive:
            history = archive.read("SP_Flash_Tool/history.ini").decode("utf-8")
            self.assertIn("lastDir=MTK_AllInOneDA.bin", history)
            self.assertIn(
                archive.read("SP_Flash_Tool/MTK_AllInOneDA.bin"), b"firmware-da"
            )
            ini = archive.read("SP_Flash_Tool/fw_package.ini").decode("utf-8")
            self.assertIn("software_name = Original Software", ini)
            self.assertEqual(archive.getinfo("SP_Flash_Tool/boot.img").compress_type, zipfile.ZIP_DEFLATED)

        self.assertGreater(result.size, 0)
        self.assertEqual(len(result.sha256), 64)

    def test_macos_layout_keeps_symlinks(self):
        out = self.tmp / "rom-macos.zip"
        result = build_portable_package(
            self.mac_tool,
            inspect_firmware(self.firmware),
            self.mac_app_platform,
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
        )
        names = names_of(out)
        self.assertIn("Install Firmware.app/Contents/MacOS/Innioasis Updater", names)
        self.assertIn("Install Firmware.app/Contents/Info.plist", names)
        self.assertIn("Firmware/boot.img", names)
        self.assertIn("Firmware/MT6572_Android_scatter.txt", names)
        self.assertIn("Firmware/fw_package.ini", names)
        self.assertIn("README.txt", names)
        self.assertNotIn("boot.img", names)
        self.assertNotIn("fw_package.ini", names)
        self.assertFalse(any(n.startswith("Innioasis Updater.app") for n in names))
        self.assert_no_macos_metadata(names)
        self.assertFalse(any("_CodeSignature" in n for n in names))
        self.assertFalse(any(n.startswith("SP_Flash_Tool/") for n in names))

        with zipfile.ZipFile(out) as archive:
            info = archive.getinfo("Install Firmware.app/Contents/Frameworks/QtCore")
            self.assertEqual(info.external_attr >> 16 & 0o170000, 0o120000)
            self.assertEqual(
                archive.read(info), b"QtCore.framework/Versions/A/QtCore"
            )
            plist = plistlib.loads(
                archive.read("Install Firmware.app/Contents/Info.plist")
            )
            self.assertEqual(plist["CFBundleName"], "Install Firmware")
            self.assertEqual(plist["CFBundleDisplayName"], "Install Firmware")
            self.assertEqual(plist["CFBundleExecutable"], "Innioasis Updater")
            readme = archive.read("README.txt").decode("utf-8")
            self.assertIn("Install Firmware.app", readme)
            self.assertIn("Firmware", readme)
        self.assertGreater(result.size, 0)

    def test_macos_dmg_layout_copies_dmg_and_lowercase_firmware(self):
        dmg = self.tmp / "Innioasis.Updater.for.Mac.dmg"
        dmg_bytes = b"signed disk image bytes"
        dmg.write_bytes(dmg_bytes)
        platform = replace(
            self.cfg.platform("macos"),
            layout="beside_dmg",
            updater_asset="Innioasis.Updater.for.Mac.dmg",
            updater_repo="y1-community/innioasis-updater",
        )
        out = self.tmp / "rom-macos-dmg.zip"
        post = b"[InternetShortcut]\r\nURL=https://example.test/steps\r\n"
        result = build_portable_package(
            dmg,
            inspect_firmware(self.firmware, source_repo="schulzcode/y2linux"),
            platform,
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
            source_repo="schulzcode/y2linux",
            post_install_name="3. After Install Steps.url",
            post_install_data=post,
        )
        names = names_of(out)
        self.assertIn("1. Install App and Firmware.dmg", names)
        self.assertIn("firmware/boot.img", names)
        self.assertIn("firmware/MT6572_Android_scatter.txt", names)
        self.assertIn("firmware/images/system.img", names)
        self.assertIn("firmware/fw_package.ini", names)
        self.assertIn("3. After Install Steps.url", names)
        self.assertIn("README.txt", names)
        self.assertFalse(any(n.startswith("Install Firmware.app") for n in names))
        self.assertFalse(any(n.startswith("Firmware/") for n in names))
        self.assertFalse(any(n.startswith("SP_Flash_Tool/") for n in names))
        self.assert_no_macos_metadata(names)
        with zipfile.ZipFile(out) as archive:
            info = archive.getinfo("1. Install App and Firmware.dmg")
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(info.compress_size, len(raw_deflate(dmg_bytes)))
            self.assertEqual(archive.read(info), dmg_bytes)
            self.assertEqual(archive.read("3. After Install Steps.url"), post)
            readme = archive.read("README.txt").decode("utf-8")
            self.assertIn("Open Anyway", readme)
            self.assertIn("Install from file...", readme)
            self.assertIn("firmware", readme)
        self.assertGreater(result.size, 0)

    def test_traversal_entries_are_skipped(self):
        bad = self.tmp / "bad.zip"
        write_zip(
            bad,
            [
                ("MT6572_Android_scatter.txt", b"scatter", FILE_MODE),
                ("../evil.txt", b"x", FILE_MODE),
                ("pkg/../../evil2.txt", b"x", FILE_MODE),
                ("boot.img", b"ok", FILE_MODE),
            ],
        )
        out = self.tmp / "bad-windows.zip"
        build_portable_package(
            self.windows_tool,
            inspect_firmware(bad),
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "1",
            "bad.zip",
        )
        names = names_of(out)
        self.assertNotIn("SP_Flash_Tool/../evil.txt", names)
        self.assertFalse(any("evil" in n for n in names))
        self.assertIn("SP_Flash_Tool/boot.img", names)

    def test_scatter_in_folder_is_stripped(self):
        nested = self.tmp / "nested.zip"
        write_zip(
            nested,
            [
                ("pkg/MT6572_Android_scatter.txt", b"scatter", FILE_MODE),
                ("pkg/boot.img", b"boot", FILE_MODE),
                ("pkg/sub/data.bin", b"data", FILE_MODE),
                ("README.md", b"outside", FILE_MODE),
            ],
        )
        package = inspect_firmware(nested)
        self.assertEqual(package.scatter_dir, "pkg")
        out = self.tmp / "nested-windows.zip"
        build_portable_package(
            self.windows_tool,
            package,
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "1",
            "nested.zip",
        )
        names = names_of(out)
        self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
        self.assertIn("SP_Flash_Tool/sub/data.bin", names)
        self.assertNotIn("SP_Flash_Tool/README.md", names)

    def test_large_entries_use_zip64(self):
        # Sparse partition images expand well past 4 GiB.  Streaming such an
        # entry into a non-ZIP64 member raises "File size too large, try using
        # force_zip64"; shrink the limit instead of writing 4 GiB.
        big = self.tmp / "big.zip"
        payload = os.urandom(16 * 1024)
        write_zip(
            big,
            [
                ("MT6572_Android_scatter.txt", b"scatter", FILE_MODE),
                ("system.img", payload, FILE_MODE),
            ],
        )
        original = zipfile.ZIP64_LIMIT
        zipfile.ZIP64_LIMIT = 4096
        try:
            out = self.tmp / "big-windows.zip"
            result = build_portable_package(
                self.windows_tool,
                inspect_firmware(big),
                self.cfg.platform("windows"),
                out,
                self.ini,
                "Original Software",
                "1",
                "big.zip",
            )
        finally:
            zipfile.ZIP64_LIMIT = original
        with zipfile.ZipFile(out) as archive:
            self.assertEqual(archive.read("SP_Flash_Tool/system.img"), payload)
        self.assertGreater(result.size, len(payload))

    def test_bad_zip_raises(self):
        broken = self.tmp / "broken.zip"
        broken.write_bytes(b"not a zip")
        with self.assertRaises(PackagingError):
            inspect_firmware(broken)

    def test_copied_entries_use_maximum_deflate(self):
        payload = deflate_payload()
        self.assertNotEqual(
            len(raw_deflate(payload, 6)),
            len(raw_deflate(payload, 9)),
            "fixture must distinguish level 6 from level 9",
        )
        compressed = self.tmp / "compressed.zip"
        write_zip(
            compressed,
            [
                ("MT6572_Android_scatter.txt", b"scatter", FILE_MODE),
                ("boot.img", payload, FILE_MODE),
            ],
        )
        out = self.tmp / "compressed-windows.zip"
        build_portable_package(
            self.windows_tool,
            inspect_firmware(compressed),
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "1",
            "compressed.zip",
        )
        with zipfile.ZipFile(out) as archive:
            info = archive.getinfo("SP_Flash_Tool/boot.img")
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(info.compress_size, len(raw_deflate(payload, 9)))

    def test_original_zip_is_repacked_and_stray_scatters_are_stripped(self):
        payload = deflate_payload()
        source = self.tmp / "stock-original.zip"
        write_zip(
            source,
            [
                ("MT6582_Android_scatter.txt", b"standard", FILE_MODE),
                ("MT6582_Android_scatter_def.txt", b"def layout", FILE_MODE),
                ("MT6582_Android_scatter_preserve_data.txt", b"stray", FILE_MODE),
                ("boot.img", payload, FILE_MODE),
                ("images/", b"", DIR_MODE),
                ("images/system.img", payload, FILE_MODE),
                ("Innioasis Updater.app/Contents/Frameworks/QtCore", b"link", LINK_MODE),
            ],
        )
        out = self.tmp / "stock-original-repacked.zip"
        result = repack_firmware_zip(source, out)
        names = names_of(out)
        self.assertIn("MT6582_Android_scatter.txt", names)
        self.assertNotIn("MT6582_Android_scatter_def.txt", names)
        self.assertNotIn("MT6582_Android_scatter_preserve_data.txt", names)
        self.assertIn("boot.img", names)
        self.assertIn("images/", names)
        self.assertIn("images/system.img", names)
        self.assertIn("Innioasis Updater.app/Contents/Frameworks/QtCore", names)
        self.assertEqual(
            result.stripped,
            [
                "MT6582_Android_scatter_def.txt",
                "MT6582_Android_scatter_preserve_data.txt",
            ],
        )
        self.assertLess(result.size, source.stat().st_size)
        with zipfile.ZipFile(out) as archive:
            info = archive.getinfo("boot.img")
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(info.compress_size, len(raw_deflate(payload, 9)))
            self.assertEqual(archive.read("boot.img"), payload)
            symlink = archive.getinfo(
                "Innioasis Updater.app/Contents/Frameworks/QtCore"
            )
            self.assertEqual(symlink.external_attr >> 16 & 0o170000, 0o120000)

    def test_original_zip_with_one_nonstandard_scatter_keeps_it(self):
        source = self.tmp / "one-scatter.zip"
        write_zip(
            source,
            [
                ("MT6582_Android_scatter_def.txt", b"only layout", FILE_MODE),
                ("boot.img", b"boot", FILE_MODE),
            ],
        )
        out = self.tmp / "one-scatter-repacked.zip"
        result = repack_firmware_zip(source, out)
        self.assertIn("MT6582_Android_scatter_def.txt", names_of(out))
        self.assertEqual(result.stripped, [])

    def test_original_zip_keeps_every_standard_scatter(self):
        source = self.tmp / "two-standard.zip"
        write_zip(
            source,
            [
                ("MT6572_Android_scatter.txt", b"y1 layout", FILE_MODE),
                ("MT6582_Android_scatter.txt", b"y2 layout", FILE_MODE),
                ("boot.img", b"boot", FILE_MODE),
            ],
        )
        out = self.tmp / "two-standard-repacked.zip"
        result = repack_firmware_zip(source, out)
        self.assertEqual(
            sorted(n for n in names_of(out) if "scatter" in n.lower()),
            ["MT6572_Android_scatter.txt", "MT6582_Android_scatter.txt"],
        )
        self.assertEqual(result.stripped, [])


if __name__ == "__main__":
    unittest.main()
