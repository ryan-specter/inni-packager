import os
import sys
import unittest
import zipfile
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
            ],
        )

        self.windows_tool = self.tmp / "windows.zip"
        write_zip(
            self.windows_tool,
            [
                ("SP_Flash_Tool/", b"", DIR_MODE),
                ("SP_Flash_Tool/flash_tool.exe", b"tool", FILE_MODE),
                ("SP_Flash_Tool/history.ini", b"[old]\n", FILE_MODE),
                ("InnioasisUpdater", b"python", FILE_MODE),
                ("_internal/base_library.zip", b"zip", FILE_MODE),
            ],
        )

        self.mac_tool = self.tmp / "mac.zip"
        write_zip(
            self.mac_tool,
            [
                ("Innioasis Updater.app/", b"", DIR_MODE),
                ("Innioasis Updater.app/Contents/MacOS/Innioasis Updater", b"binary", FILE_MODE),
                ("Innioasis Updater.app/Contents/Frameworks/QtCore", b"QtCore.framework/Versions/A/QtCore", LINK_MODE),
            ],
        )

        self.ini = mirrorspec.fw_package_ini(
            "Original Software", "3.1.2", "Y1", "Type A"
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inspect_picks_non_preserve_scatter(self):
        package = inspect_firmware(self.firmware)
        self.assertEqual(package.scatter_dir, "")
        self.assertEqual(package.chosen_scatter, "MT6572_Android_scatter.txt")
        self.assertEqual(len(package.scatters), 2)

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
        self.assertEqual(names.count("SP_Flash_Tool/history.ini"), 1)

        with zipfile.ZipFile(out) as archive:
            history = archive.read("SP_Flash_Tool/history.ini").decode("utf-8")
            self.assertIn("scatterHistory=MT6572_Android_scatter.txt", history)
            self.assertNotIn("[old]", history)
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
            self.cfg.platform("macos"),
            out,
            self.ini,
            "Original Software",
            "3.1.2",
            "rom.zip",
        )
        names = names_of(out)
        self.assertIn("Innioasis Updater.app/Contents/MacOS/Innioasis Updater", names)
        self.assertIn("boot.img", names)
        self.assertIn("MT6572_Android_scatter.txt", names)
        self.assertIn("fw_package.ini", names)
        self.assertIn("README.txt", names)
        self.assertFalse(any(n.startswith("SP_Flash_Tool/") for n in names))

        with zipfile.ZipFile(out) as archive:
            info = archive.getinfo("Innioasis Updater.app/Contents/Frameworks/QtCore")
            self.assertEqual(info.external_attr >> 16 & 0o170000, 0o120000)
            self.assertEqual(
                archive.read(info), b"QtCore.framework/Versions/A/QtCore"
            )
            readme = archive.read("README.txt").decode("utf-8")
            self.assertIn("Innioasis Updater.app", readme)
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

    def test_bad_zip_raises(self):
        broken = self.tmp / "broken.zip"
        broken.write_bytes(b"not a zip")
        with self.assertRaises(PackagingError):
            inspect_firmware(broken)


if __name__ == "__main__":
    unittest.main()
