import os
import plistlib
import sys
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mirrorspec  # noqa: E402
from packager import (  # noqa: E402
    PackagingError,
    build_portable_package,
    inspect_firmware,
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

    def test_y2_repos_drop_preserve_txt(self):
        for repo in ("schulzcode/y2linux", "schulzcode/Y2Reborn"):
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
            self.assertNotIn("SP_Flash_Tool/preserve_data_scatter.txt", names)
            self.assertIn("SP_Flash_Tool/MT6572_Android_scatter.txt", names)
            with zipfile.ZipFile(out) as archive:
                history = archive.read("SP_Flash_Tool/history.ini").decode("utf-8")
            self.assertIn("scatterHistory=MT6572_Android_scatter.txt", history)

        mac_out = self.tmp / "rom-macos.zip"
        build_portable_package(
            self.mac_tool,
            inspect_firmware(self.firmware, source_repo="schulzcode/y2linux"),
            self.mac_app_platform,
            mac_out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
            source_repo="schulzcode/y2linux",
        )
        mac_names = names_of(mac_out)
        self.assertNotIn("Firmware/preserve_data_scatter.txt", mac_names)
        self.assertIn("Firmware/MT6572_Android_scatter.txt", mac_names)

        package = inspect_firmware(self.firmware, source_repo="schulzcode/other")
        self.assertEqual(package.scatters, ["MT6572_Android_scatter.txt"])
        out = self.tmp / "rom-other.zip"
        build_portable_package(
            self.windows_tool,
            package,
            self.cfg.platform("windows"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
            source_repo="schulzcode/other",
        )
        self.assertIn("SP_Flash_Tool/preserve_data_scatter.txt", names_of(out))

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
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
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


if __name__ == "__main__":
    unittest.main()
