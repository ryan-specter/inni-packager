import Foundation

enum NativeSelfTest {
    static func run() async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory
            .appendingPathComponent("MirrorWatch-selftest-\(UUID().uuidString)", isDirectory: true)
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: root) }

        let versionCases: [(tag: String, title: String, expected: String)] = [
            ("baseline-215", "Y2Linux 1.0 Beta 9.3.7", "9.3.7"),
            ("build-20241010", "1.0.4 - Initial Release", "1.0.4"),
            ("git-tag-98", "0.6 Rockbox (ADB) (Wi-Fi)", "0.6"),
            ("v2.5.1", "Innioasis Updater CE", "2.5.1"),
            ("20261013", "rom_y1_type_a", "October 13 2026"),
            ("20260716-0131", "rom", "July 16 2026"),
            ("baseline-215", "rom_y2.zip", "baseline-215"),
        ]
        for item in versionCases {
            let actual = NativePackager.releaseVersion(item.tag, title: item.title)
            guard actual == item.expected else {
                throw selfTestError(
                    "release version \(item.tag)/\(item.title): \(actual) != \(item.expected)"
                )
            }
        }

        let manifest = """
        <?xml version="1.0" encoding="UTF-8"?>
        <manifest>
          <package name="Demo" repo="Example/Test-ROM" device="Y1" type="img" />
          <package name="Demo" repo="Example/Test-ROM" device="Y2" type="img" />
          <package name="App" repo="Example/Test-ROM" device="Y1" type="apk" />
          <package name="Bad" repo="not-a-repository" type="img" />
        </manifest>
        """
        let packages = try ManifestParser.parse(Data(manifest.utf8))
        guard packages.count == 2 else {
            throw selfTestError("manifest parser returned \(packages.count) packages")
        }
        let products = try ManifestService.products(
            from: Data(manifest.utf8),
            organization: "y1-community"
        )
        guard products.count == 1,
              products[0].mirrorRepo == "y1-community/test-rom-mirror" else {
            throw selfTestError("manifest product grouping failed")
        }
        let config = ManifestService.generatedConfig(products[0], organization: "y1-community")
        guard config.contains("\"source_repo\" : \"Example/Test-ROM\""),
              config.contains("\"mirror_repo\" : \"y1-community/test-rom-mirror\""),
              config.contains("\"macos\""),
              config.contains("Innioasis.Updater.for.Mac.dmg"),
              config.contains("beside_dmg") else {
            throw selfTestError("generated mirror config is incomplete")
        }

        let portableRoot = root.appendingPathComponent("portable", isDirectory: true)
        let app = portableRoot.appendingPathComponent("Test.app", isDirectory: true)
        let contents = app.appendingPathComponent("Contents", isDirectory: true)
        let macos = contents.appendingPathComponent("MacOS", isDirectory: true)
        try manager.createDirectory(at: macos, withIntermediateDirectories: true)
        let executable = macos.appendingPathComponent("Test")
        try Data("#!/bin/sh\n".utf8).write(to: executable)
        try manager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        try manager.createSymbolicLink(
            atPath: macos.appendingPathComponent("test-link").path,
            withDestinationPath: "Test"
        )
        let plist: [String: Any] = ["CFBundleName": "Test", "CFBundleExecutable": "Test"]
        let plistData = try PropertyListSerialization.data(
            fromPropertyList: plist,
            format: .xml,
            options: 0
        )
        try plistData.write(to: contents.appendingPathComponent("Info.plist"))
        try manager.createDirectory(
            at: portableRoot.appendingPathComponent("SP_Flash_Tool"),
            withIntermediateDirectories: true
        )
        try Data("da".utf8).write(
            to: portableRoot.appendingPathComponent("SP_Flash_Tool/MTK_AllInOne_DA.bin")
        )
        let portableZip = root.appendingPathComponent("portable.zip")
        _ = try await NativeShell.require(
            "/usr/bin/zip",
            arguments: [
                "-9", "-r", "-y", "-q", "-X", portableZip.path,
                "Test.app", "SP_Flash_Tool",
            ],
            workingDirectory: portableRoot
        )

        let firmwareRoot = root.appendingPathComponent("firmware", isDirectory: true)
        let rom = firmwareRoot.appendingPathComponent("rom", isDirectory: true)
        try manager.createDirectory(at: rom, withIntermediateDirectories: true)
        let scatter = """
        - partition_index: SYS0
          partition_name: boot
          file_name: boot.img
          is_download: true
        - partition_index: SYS1
          partition_name: system
          file_name: system.img
          is_download: true
        - partition_index: SYS2
          partition_name: unused
          file_name: unused.bin
          is_download: false
        """
        try Data(scatter.utf8).write(to: rom.appendingPathComponent("scatter.txt"))
        try Data("boot".utf8).write(to: rom.appendingPathComponent("boot.img"))
        try Data("system".utf8).write(to: rom.appendingPathComponent("system.img"))
        try Data("unused".utf8).write(to: rom.appendingPathComponent("unused.bin"))
        let firmwareZip = root.appendingPathComponent("firmware.zip")
        _ = try await NativeShell.require(
            "/usr/bin/zip",
            arguments: ["-9", "-r", "-q", "-X", firmwareZip.path, "rom"],
            workingDirectory: firmwareRoot
        )
        let archive = try await NativePackager.inspectFirmware(firmwareZip)
        guard archive.hasScatter else {
            throw selfTestError("firmware scatter was not detected")
        }
        let ini = NativePackager.fwPackageIni(
            ResolvedFirmwareMetadata(
                softwareName: "Demo",
                targetModel: "Y1",
                targetType: "Type A",
                description: "Test"
            ),
            version: "1.0"
        )
        let macPlatform = MirrorPlatform(
            key: "macos",
            suffix: "-macos.zip",
            updaterAsset: "Innioasis.Updater.for.Mac.dmg",
            layout: "beside_dmg",
            os: "macos",
            arch: "universal",
            optional: false,
            pinnedTag: ""
        )
        let macDMG = root.appendingPathComponent(NativePackager.macUpdaterAssetName)
        try Data("fake-dmg".utf8).write(to: macDMG)
        let macOutput = root.appendingPathComponent("rom-macos.zip")
        _ = try await NativePackager.buildPackage(
            portableZip: macDMG,
            firmware: archive,
            platform: macPlatform,
            output: macOutput,
            iniText: ini,
            softwareName: "Demo",
            version: "1.0",
            sourceAsset: "rom.zip",
            sourceRepo: "Example/Test-ROM"
        )
        let macExtract = root.appendingPathComponent("mac-extract", isDirectory: true)
        _ = try await NativeShell.require(
            "/usr/bin/ditto",
            arguments: ["-x", "-k", macOutput.path, macExtract.path]
        )
        let macArtifact = macExtract.appendingPathComponent(NativePackager.macUpdaterAssetName)
        let macFirmware = macExtract.appendingPathComponent(NativePackager.macFirmwareFolder)
        let macReadme = macExtract.appendingPathComponent("README.txt")
        let macRootNames = try manager.contentsOfDirectory(atPath: macExtract.path)
        guard manager.fileExists(atPath: macArtifact.path),
              manager.fileExists(atPath: macFirmware.appendingPathComponent("scatter.txt").path),
              manager.fileExists(atPath: macFirmware.appendingPathComponent("boot.img").path),
              manager.fileExists(atPath: macFirmware.appendingPathComponent("system.img").path),
              manager.fileExists(atPath: macFirmware.appendingPathComponent("fw_package.ini").path),
              manager.fileExists(atPath: macReadme.path),
              !macRootNames.contains("Install Firmware.app"),
              !macRootNames.contains("Firmware"),
              !manager.fileExists(atPath: macFirmware.appendingPathComponent("unused.bin").path) else {
            throw selfTestError("macOS package layout is wrong")
        }
        let readmeText = try String(contentsOf: macReadme, encoding: .utf8)
        guard readmeText.contains("System Settings > Privacy & Security"),
              readmeText.contains("Open Anyway"),
              readmeText.contains("Install from file..."),
              readmeText.contains(NativePackager.macUpdaterAssetName) else {
            throw selfTestError("macOS README is missing the DMG instructions")
        }

        let windowsPlatform = MirrorPlatform(
            key: "windows",
            suffix: "-windows.zip",
            updaterAsset: "windows.zip",
            layout: "sp_flash_tool",
            os: "windows",
            arch: "x86-64",
            optional: false,
            pinnedTag: ""
        )
        let windowsOutput = root.appendingPathComponent("rom-windows.zip")
        _ = try await NativePackager.buildPackage(
            portableZip: portableZip,
            firmware: archive,
            platform: windowsPlatform,
            output: windowsOutput,
            iniText: ini,
            softwareName: "Demo",
            version: "1.0",
            sourceAsset: "rom.zip",
            sourceRepo: "Example/Test-ROM"
        )
        let windowsExtract = root.appendingPathComponent("windows-extract", isDirectory: true)
        _ = try await NativeShell.require(
            "/usr/bin/ditto",
            arguments: ["-x", "-k", windowsOutput.path, windowsExtract.path]
        )
        let sp = windowsExtract.appendingPathComponent("SP_Flash_Tool")
        guard manager.fileExists(atPath: sp.appendingPathComponent("boot.img").path),
              manager.fileExists(atPath: sp.appendingPathComponent("scatter.txt").path),
              manager.fileExists(atPath: sp.appendingPathComponent("MTK_AllInOne_DA.bin").path),
              manager.fileExists(atPath: sp.appendingPathComponent("fw_package.ini").path),
              manager.fileExists(atPath: sp.appendingPathComponent("history.ini").path),
              manager.fileExists(atPath: windowsExtract.appendingPathComponent("fw_package.ini").path) else {
            throw selfTestError("Windows package layout is wrong")
        }
        let historyText = try String(contentsOf: sp.appendingPathComponent("history.ini"), encoding: .utf8)
        guard historyText.contains("lastDir=MTK_AllInOne_DA.bin"),
              historyText.contains("scatterHistory=scatter.txt"),
              !historyText.contains(":\\") else {
            throw selfTestError("SP Flash Tool history is not using bare filenames")
        }
        let entries = try await NativeShell.require("/usr/bin/unzip", arguments: ["-Z1", macOutput.path])
        let listing = String(decoding: entries.stdout, as: UTF8.self)
        guard !listing.contains("__MACOSX") else {
            throw selfTestError("package contains __MACOSX")
        }
        print("Native packager self-test passed")
    }

    private static func selfTestError(_ message: String) -> NSError {
        NSError(
            domain: "MirrorWatch.SelfTest",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: message]
        )
    }
}
