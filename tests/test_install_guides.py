import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import install_guides  # noqa: E402


class InstallGuideTests(unittest.TestCase):
    def test_windows_guide_follows_the_required_flow(self):
        text = install_guides.render_text(
            "windows-legacy",
            software="Original Software",
            version="3.1.2",
            model="Y1",
            target_type="Type A",
            firmware_asset="rom.zip",
            scatter="MT6572_Android_scatter.txt",
        )
        for phrase in (
            "Driver folder",
            "Reboot the PC",
            "SP_Flash_Tool",
            "flash_tool.exe",
            "Download",
            "powered off",
            "connect the player",
            "completion message",
        ):
            self.assertIn(phrase, text)
        self.assertIn("Original Software 3.1.2 - Y1 Type A", text)
        self.assertIn("rom.zip", text)
        self.assertIn("MT6572_Android_scatter.txt", text)
        self.assertIn("legacy-windows.html", text)

    def test_linux_guide_targets_the_32_bit_tool(self):
        text = install_guides.render_text(
            "linux-legacy",
            software="Rockbox",
            version="0.6",
            model="Y1",
            target_type="Type B",
            firmware_asset="rom_type_b.zip",
        )
        self.assertIn("./flash_tool", text)
        self.assertIn("32-bit", text)
        self.assertIn("Y1 Type B", text)
        self.assertIn("legacy-linux-32.html", text)
        self.assertNotIn("flash_tool.exe", text)

    def test_render_markdown_is_a_static_page(self):
        text = install_guides.render_markdown(
            "windows-legacy", model="Y2", target_type="Stock"
        )
        self.assertTrue(text.startswith("# "))
        self.assertIn("### 1. Extract the download", text)
        self.assertIn("Y2 Stock", text)

    def test_static_pages_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = install_guides.write_static_guides(tmp)
            names = {path.name for path in paths}
            self.assertEqual(
                names, {"index.md", "legacy-windows.md", "legacy-linux-32.md"}
            )
            self.assertTrue(all(path.is_file() for path in paths))
            index = (Path(tmp) / "index.md").read_text(encoding="utf-8")
        self.assertIn("legacy-windows.md", index)
        self.assertIn("legacy-linux-32.md", index)

    def test_guide_url_carries_the_device_permutation(self):
        url = install_guides.guide_url(
            "windows-legacy",
            model="Y1",
            target_type="Type B",
            tag="3.1.2",
            software="Rockbox",
        )
        self.assertIn("legacy-windows.html", url)
        self.assertIn("model=Y1", url)
        self.assertIn("type=Type+B", url)
        self.assertIn("tag=3.1.2", url)
        self.assertEqual(install_guides.guide_url("windows"), "")
        self.assertEqual(install_guides.guide_url("macos"), "")

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(KeyError):
            install_guides.render_text("amiga")


if __name__ == "__main__":
    unittest.main()
