import CryptoKit
import Foundation

enum NativePackagingError: LocalizedError {
    case tool(String, Int32, String)
    case badZip(String)
    case missingScatter(String)
    case missingFirmware(String)
    case noApplication(String)
    case invalidDMG(String)
    case emptyPackage(String)

    var errorDescription: String? {
        switch self {
        case let .tool(command, status, message):
            return "\(command) exited with \(status): \(message)"
        case let .badZip(name):
            return "\(name) is not a readable zip."
        case let .missingScatter(name):
            return "\(name) has no scatter file."
        case let .missingFirmware(message):
            return message
        case let .noApplication(name):
            return "\(name) has no macOS .app bundle."
        case let .invalidDMG(name):
            return "\(name) is not the expected macOS DMG."
        case let .emptyPackage(name):
            return "\(name) produced an empty archive."
        }
    }
}

struct ShellResult: Sendable {
    let status: Int32
    let stdout: Data
    let stderr: String
}

enum NativeShell {
    static func run(
        _ executable: String,
        arguments: [String],
        workingDirectory: URL? = nil,
        stdoutTo: URL? = nil
    ) async throws -> ShellResult {
        try await Task.detached(priority: .userInitiated) {
            let manager = FileManager.default
            let temp = manager.temporaryDirectory
                .appendingPathComponent("MirrorWatch-tool-\(UUID().uuidString)", isDirectory: true)
            try manager.createDirectory(at: temp, withIntermediateDirectories: true)
            defer { try? manager.removeItem(at: temp) }

            let outputURL = stdoutTo ?? temp.appendingPathComponent("stdout")
            try manager.createDirectory(
                at: outputURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if manager.fileExists(atPath: outputURL.path) {
                try manager.removeItem(at: outputURL)
            }
            guard manager.createFile(atPath: outputURL.path, contents: nil) else {
                throw NativePackagingError.tool(executable, -1, "could not create output file")
            }
            let errorURL = temp.appendingPathComponent("stderr")
            guard manager.createFile(atPath: errorURL.path, contents: nil) else {
                throw NativePackagingError.tool(executable, -1, "could not create error file")
            }
            let output = try FileHandle(forWritingTo: outputURL)
            let errors = try FileHandle(forWritingTo: errorURL)
            defer {
                try? output.close()
                try? errors.close()
            }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = arguments
            process.currentDirectoryURL = workingDirectory
            process.standardOutput = output
            process.standardError = errors
            process.standardInput = FileHandle.nullDevice
            process.environment = ProcessInfo.processInfo.environment
            do {
                try process.run()
            } catch {
                throw NativePackagingError.tool(executable, -1, error.localizedDescription)
            }
            process.waitUntilExit()
            try? output.synchronize()
            try? errors.synchronize()
            let stderr = (try? String(contentsOf: errorURL, encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let stdout = stdoutTo == nil ? (try? Data(contentsOf: outputURL)) ?? Data() : Data()
            return ShellResult(status: process.terminationStatus, stdout: stdout, stderr: stderr)
        }.value
    }

    static func require(
        _ executable: String,
        arguments: [String],
        workingDirectory: URL? = nil
    ) async throws -> ShellResult {
        let result = try await run(executable, arguments: arguments, workingDirectory: workingDirectory)
        guard result.status == 0 else {
            throw NativePackagingError.tool(
                URL(fileURLWithPath: executable).lastPathComponent,
                result.status,
                result.stderr
            )
        }
        return result
    }
}

struct ZipEntry: Sendable {
    let archivePath: String
    let path: String
    let isDirectory: Bool
}

struct FirmwareItem: Sendable {
    let archivePath: String
    let relativePath: String
}

struct FirmwareArchive: Sendable {
    let url: URL
    let entries: [ZipEntry]
    let scatterDir: String
    let scatters: [String]
    let chosenScatter: String?

    var hasScatter: Bool { chosenScatter != nil }

    func plan(sourceRepo: String) async throws -> [FirmwareItem] {
        guard hasScatter else {
            if scatters.count > 1 {
                throw NativePackagingError.missingFirmware(
                    "\(url.lastPathComponent) has \(scatters.count) scatter files; "
                        + "a portable package must carry exactly one"
                )
            }
            throw NativePackagingError.missingScatter(url.lastPathComponent)
        }
        var required = Set<String>()
        if let chosenScatter {
            let chosenPath = scatterDir.isEmpty ? chosenScatter : "\(scatterDir)/\(chosenScatter)"
            if let entry = entries.first(where: { $0.path == chosenPath }),
               let text = try await NativePackager.readTextEntry(zip: url, entry: entry) {
                required = Set(NativePackager.scatterListedFiles(text).map { $0.lowercased() })
            }
        }
        let prefix = scatterDir.isEmpty ? "" : scatterDir + "/"
        let prefixLower = prefix.lowercased()
        var present = Set<String>()
        var result: [FirmwareItem] = []
        for entry in entries where !entry.isDirectory {
            let relative: String
            if prefix.isEmpty {
                relative = entry.path
            } else {
                guard entry.path.lowercased().hasPrefix(prefixLower) else { continue }
                relative = String(entry.path.dropFirst(prefix.count))
            }
            guard !relative.isEmpty else { continue }
            present.insert(relative.lowercased())
            let base = (relative as NSString).lastPathComponent
            let lower = base.lowercased()
            // Exactly one scatter travels with the package: the one the
            // firmware zip declares.  Never copy preserve-data or alternates.
            if lower.hasSuffix(".txt"), lower.contains("scatter"),
               lower != chosenScatter?.lowercased() {
                continue
            }
            if lower == "history.ini" || lower == "fw_package.ini" { continue }
            if !required.isEmpty {
                let path = relative.lowercased()
                if path != chosenScatter?.lowercased(),
                   !required.contains(path),
                   !NativePackager.isOptionalPackageFile(relative) {
                    continue
                }
            }
            result.append(FirmwareItem(archivePath: entry.archivePath, relativePath: relative))
        }
        let missing = required.subtracting(present)
        if !missing.isEmpty {
            throw NativePackagingError.missingFirmware(
                "\(url.lastPathComponent) is missing: \(missing.sorted().joined(separator: ", "))"
            )
        }
        return result.sorted { $0.relativePath < $1.relativePath }
    }
}

struct ResolvedFirmwareMetadata: Sendable {
    var softwareName = ""
    var targetModel = ""
    var targetType = ""
    var description = ""
}

struct NativePackageResult: Sendable {
    let name: String
    let url: URL
    let size: Int
    let sha256: String
}

enum NativePackager {
    static let macUpdaterAssetName = "1. Install App and Firmware.dmg"
    static let macFirmwareFolder = "firmware"

    static func inspectFirmware(_ url: URL) async throws -> FirmwareArchive {
        let entries = try await listEntries(zip: url)
        let scatterEntries = entries.filter {
            !$0.isDirectory
                && $0.path.lowercased().hasSuffix(".txt")
                && ($0.path as NSString).lastPathComponent.lowercased().contains("scatter")
                && !($0.path as NSString).lastPathComponent.lowercased().contains("preserve")
        }
        var groups: [String: [String]] = [:]
        for entry in scatterEntries {
            groups[(entry.path as NSString).deletingLastPathComponent, default: []].append(entry.path)
        }
        guard !groups.isEmpty else {
            return FirmwareArchive(url: url, entries: entries, scatterDir: "", scatters: [], chosenScatter: nil)
        }
        let dir: String
        if groups[""] != nil {
            dir = ""
        } else {
            dir = groups.keys.sorted {
                let lc = groups[$0]?.count ?? 0
                let rc = groups[$1]?.count ?? 0
                if lc != rc { return lc > rc }
                return $0.count < $1.count
            }.first ?? ""
        }
        let scatters = (groups[dir] ?? []).map { ($0 as NSString).lastPathComponent }
        let android = scatters.filter { $0.lowercased().contains("ndroid_scatter") }
        let chosen: String?
        if android.count == 1 {
            chosen = android[0]
        } else if scatters.count == 1 {
            chosen = scatters[0]
        } else {
            chosen = nil
        }
        return FirmwareArchive(
            url: url,
            entries: entries,
            scatterDir: dir,
            scatters: scatters,
            chosenScatter: chosen
        )
    }

    static func buildPackage(
        portableZip: URL,
        firmware: FirmwareArchive,
        platform: MirrorPlatform,
        output: URL,
        iniText: String,
        softwareName: String,
        version: String,
        sourceAsset: String,
        sourceRepo: String
    ) async throws -> NativePackageResult {
        let manager = FileManager.default
        let stage = manager.temporaryDirectory
            .appendingPathComponent("MirrorWatch-package-\(UUID().uuidString)", isDirectory: true)
        try manager.createDirectory(at: stage, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: stage) }
        let items = try await firmware.plan(sourceRepo: sourceRepo)
        if platform.isDMGBundle {
            guard portableZip.pathExtension.lowercased() == "dmg" else {
                throw NativePackagingError.invalidDMG(portableZip.lastPathComponent)
            }
            let target = stage.appendingPathComponent(Self.macUpdaterAssetName)
            if manager.fileExists(atPath: target.path) {
                try manager.removeItem(at: target)
            }
            try manager.copyItem(at: portableZip, to: target)
            let firmwareRoot = stage.appendingPathComponent(Self.macFirmwareFolder, isDirectory: true)
            if manager.fileExists(atPath: firmwareRoot.path) {
                try manager.removeItem(at: firmwareRoot)
            }
            try manager.createDirectory(at: firmwareRoot, withIntermediateDirectories: true)
            for item in items {
                try await extractEntry(
                    zip: firmware.url,
                    entry: item.archivePath,
                    to: firmwareRoot.appendingPathComponent(item.relativePath)
                )
            }
            try Data(iniText.utf8).write(
                to: firmwareRoot.appendingPathComponent("fw_package.ini"),
                options: .atomic
            )
            let readme = macosReadme(
                dmgName: Self.macUpdaterAssetName,
                firmwareFolder: Self.macFirmwareFolder,
                firmwareAsset: sourceAsset,
                software: softwareName,
                version: version
            )
            try Data(readme.utf8).write(
                to: stage.appendingPathComponent("README.txt"),
                options: .atomic
            )
        } else {
            try await extractZip(portableZip, to: stage)
            removeJunk(in: stage)
            let top = try manager.contentsOfDirectory(
                at: stage,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )
            let sp = top.first {
                $0.lastPathComponent.lowercased() == "sp_flash_tool"
                    && (try? $0.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
            } ?? stage.appendingPathComponent("SP_Flash_Tool", isDirectory: true)
            try manager.createDirectory(at: sp, withIntermediateDirectories: true)
            for item in items {
                try await extractEntry(
                    zip: firmware.url,
                    entry: item.archivePath,
                    to: sp.appendingPathComponent(item.relativePath)
                )
            }
            try Data(iniText.utf8).write(
                to: stage.appendingPathComponent("fw_package.ini"),
                options: .atomic
            )
            try Data(iniText.utf8).write(
                to: sp.appendingPathComponent("fw_package.ini"),
                options: .atomic
            )
            let scatter = firmware.chosenScatter ?? "scatter.txt"
            let toolFiles = (try? manager.contentsOfDirectory(atPath: sp.path)) ?? []
            let preferredDA = ["MTK_AllInOne_DA.bin", "MTK_AllInOneDA.bin"]
            let daName = preferredDA.first { candidate in
                toolFiles.contains { $0.caseInsensitiveCompare(candidate) == .orderedSame }
            } ?? "MTK_AllInOne_DA.bin"
            let history = "[LastDAFilePath]\r\nlastDir=\(daName)\r\n\r\n"
                + "[RecentOpenFile]\r\nlastDir=\r\nscatterHistory=\(scatter)\r\n"
                + "authHistory=@Invalid()\r\n"
            try Data(history.utf8).write(
                to: sp.appendingPathComponent("history.ini"),
                options: .atomic
            )
        }

        removeJunk(in: stage)
        let names = try manager.contentsOfDirectory(atPath: stage.path)
            .filter { $0 != ".DS_Store" && !isMacMetadata($0) }
        guard !names.isEmpty else {
            throw NativePackagingError.emptyPackage(output.lastPathComponent)
        }
        try manager.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
        if manager.fileExists(atPath: output.path) {
            try manager.removeItem(at: output)
        }
        var arguments = ["-9", "-r", "-y", "-q", "-X", output.path]
        arguments.append(contentsOf: names)
        _ = try await NativeShell.require("/usr/bin/zip", arguments: arguments, workingDirectory: stage)
        let size = (try? manager.attributesOfItem(atPath: output.path)[.size] as? Int) ?? 0
        guard size > 0 else { throw NativePackagingError.emptyPackage(output.lastPathComponent) }
        return NativePackageResult(
            name: output.lastPathComponent,
            url: output,
            size: size,
            sha256: try sha256(output)
        )
    }

    static func resolveMetadata(
        config: MirrorConfig,
        assetName: String,
        releaseName: String
    ) -> ResolvedFirmwareMetadata {
        var result = ResolvedFirmwareMetadata(
            softwareName: config.defaults?.softwareName ?? "",
            targetModel: config.defaults?.targetModel ?? "",
            targetType: config.defaults?.targetType ?? "",
            description: config.defaults?.description ?? ""
        )
        if let rule = config.rules?.first(where: { $0.matches(assetName) }) {
            if let v = rule.softwareName, !v.isEmpty { result.softwareName = v }
            if let v = rule.targetModel, !v.isEmpty { result.targetModel = v }
            if let v = rule.targetType, !v.isEmpty { result.targetType = v }
            if let v = rule.description, !v.isEmpty { result.description = v }
        }
        if result.description.isEmpty { result.description = releaseName }
        return result
    }

    static func fwPackageIni(_ metadata: ResolvedFirmwareMetadata, version: String) -> String {
        var lines = ["[Firmware]"]
        func add(_ key: String, _ value: String) {
            let clean = value.components(separatedBy: .newlines).joined(separator: " ")
                .trimmingCharacters(in: .whitespaces)
            if !clean.isEmpty { lines.append("\(key) = \(clean)") }
        }
        add("software_name", metadata.softwareName)
        add("version", version)
        add("target_model", metadata.targetModel)
        add("target_type", metadata.targetType)
        add("description", metadata.description)
        lines.append("enabled = true")
        return lines.joined(separator: "\r\n") + "\r\n"
    }

    static func releaseVersion(_ tag: String, title: String? = nil) -> String {
        let cleanTag = tag.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanTitle = (title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if let version = titleStartOrEndSemver(cleanTitle) { return version }
        if let version = tagSemver(cleanTag) { return version }
        if let version = datestampLabel(cleanTag) { return version }
        if let version = semverFromReleaseTitle(cleanTitle) { return version }
        if cleanTag.count > 1, cleanTag.first == "v" || cleanTag.first == "V" {
            return String(cleanTag.dropFirst())
        }
        return cleanTag
    }

    private static func firstCapture(
        _ pattern: String,
        in text: String,
        options: NSRegularExpression.Options = [.caseInsensitive]
    ) -> String? {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: options) else {
            return nil
        }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        guard let match = regex.firstMatch(in: text, options: [], range: range),
              match.numberOfRanges > 1,
              let capture = Range(match.range(at: 1), in: text) else {
            return nil
        }
        return String(text[capture])
    }

    private static func matchGroups(
        _ pattern: String,
        in text: String,
        options: NSRegularExpression.Options = [.caseInsensitive]
    ) -> [String]? {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: options) else {
            return nil
        }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        guard let match = regex.firstMatch(in: text, options: [], range: range) else {
            return nil
        }
        var groups: [String] = []
        for index in 1..<match.numberOfRanges {
            guard let groupRange = Range(match.range(at: index), in: text) else {
                return nil
            }
            groups.append(String(text[groupRange]))
        }
        return groups
    }

    private static func replacingMatches(
        _ pattern: String,
        in text: String,
        with template: String
    ) -> String {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else {
            return text
        }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.stringByReplacingMatches(
            in: text,
            options: [],
            range: range,
            withTemplate: template
        )
    }

    private static func titleStartOrEndSemver(_ title: String) -> String? {
        let text = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let start = #"^(?:release\s+|version\s+)?(?:\[|\()?(?:v|v\.)?(\d+(?:\.\d+)+)(?:\]|\))?(?:$|[\s:\-_|,])"#
        if let version = firstCapture(start, in: text) { return version }

        var candidate = text
        for _ in 0..<4 {
            let cleaned = replacingMatches(
                #"[\s\-_]*[\(\[](?:beta\d*|alpha\d*|rc\d*|preview|240p|360p|adb|wifi|wi-fi|root|magisk)[\)\]]\s*$"#,
                in: candidate,
                with: ""
            )
            if cleaned == candidate { break }
            candidate = cleaned.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        let end = #"(?:^|[\s:\-_|,\(\[])(?:v|v\.)?(\d+(?:\.\d+)+)(?:\]|\))?\s*$"#
        return firstCapture(end, in: candidate)
    }

    private static func tagSemver(_ tag: String) -> String? {
        let text = tag.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let compound = #"(?:^|[^\d.])(?:v|v\.)?(\d+\.\d+(?:\.\d+)?)[-_](?:[a-zA-Z0-9]+[-_]|os[-_])?v?(\d+\.\d+(?:\.\d+)?)(?:[^\d.]|$)"#
        if let version = firstCapture(compound, in: text) { return version }
        let simple = #"(?:^|[^\d.])(?:v|v\.)?(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z0-9]+)?)(?:[^\d.]|$)"#
        return firstCapture(simple, in: text)
    }

    private static func semverFromReleaseTitle(_ title: String) -> String? {
        let text = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if let version = titleStartOrEndSemver(text) { return version }
        if let version = firstCapture(
            #"\b(?:beta|alpha|rc)\s*v?(\d+(?:\.\d+)+)\b"#,
            in: text
        ) {
            return version
        }
        let pattern = #"(?<![\d.])(\d+\.\d+(?:\.\d+)+)(?![\d.])"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: []) else {
            return nil
        }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        let matches = regex.matches(in: text, options: [], range: range)
        guard matches.count == 1,
              let capture = Range(matches[0].range(at: 1), in: text) else {
            return nil
        }
        return String(text[capture])
    }

    private static func datestampLabel(_ raw: String) -> String? {
        let text = String(raw)
        let full = #"(?<!\d)((?:19|20)\d{2})[-_./]?(0[1-9]|1[0-2])[-_./]?(0[1-9]|[12]\d|3[01])(?!\d)"#
        var year: Int?
        var month: Int?
        var day: Int?
        if let groups = matchGroups(full, in: text), groups.count == 3 {
            year = Int(groups[0])
            month = Int(groups[1])
            day = Int(groups[2])
        } else {
            let short = #"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"#
            if let groups = matchGroups(short, in: text), groups.count == 3,
               let yy = Int(groups[0]), let mm = Int(groups[1]), let dd = Int(groups[2]) {
                year = yy < 70 ? 2000 + yy : 1900 + yy
                month = mm
                day = dd
            }
        }
        guard let resolvedYear = year, let resolvedMonth = month, let resolvedDay = day else {
            return nil
        }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0) ?? calendar.timeZone
        var components = DateComponents()
        components.calendar = calendar
        components.timeZone = calendar.timeZone
        components.year = resolvedYear
        components.month = resolvedMonth
        components.day = resolvedDay
        guard let date = calendar.date(from: components) else { return nil }
        let check = calendar.dateComponents([.year, .month, .day], from: date)
        guard check.year == resolvedYear, check.month == resolvedMonth, check.day == resolvedDay else {
            return nil
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "MMMM d yyyy"
        return formatter.string(from: date)
    }

    static func readTextEntry(zip: URL, entry: ZipEntry) async throws -> String? {
        let result = try await NativeShell.run("/usr/bin/unzip", arguments: ["-p", zip.path, entry.archivePath])
        guard result.status == 0 else { return nil }
        return String(data: result.stdout, encoding: .utf8)
    }

    static func listEntries(zip: URL) async throws -> [ZipEntry] {
        let result = try await NativeShell.run("/usr/bin/unzip", arguments: ["-Z1", zip.path])
        guard result.status == 0 else { throw NativePackagingError.badZip(zip.lastPathComponent) }
        guard let text = String(data: result.stdout, encoding: .utf8) else {
            throw NativePackagingError.badZip(zip.lastPathComponent)
        }
        return text.split(whereSeparator: \.isNewline).compactMap { line in
            let raw = String(line)
            guard let path = normalizedZipPath(raw) else { return nil }
            return ZipEntry(archivePath: raw, path: path, isDirectory: raw.hasSuffix("/"))
        }
    }

    static func sha256(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let data = try handle.read(upToCount: 1024 * 1024) ?? Data()
            if data.isEmpty { break }
            hasher.update(data: data)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func scatterListedFiles(_ text: String) -> Set<String> {
        var listed = Set<String>()
        var current: [String: String] = [:]
        func flush() {
            guard current["is_download"]?.lowercased() == "true",
                  var name = current["file_name"]?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !name.isEmpty, name.uppercased() != "NONE" else { return }
            name = name.replacingOccurrences(of: "\\", with: "/")
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            let parts = name.split(separator: "/", omittingEmptySubsequences: true)
            if name.hasPrefix("/") || parts.isEmpty || parts.contains(where: { $0 == ".." }) { return }
            listed.insert(parts.joined(separator: "/"))
        }
        for raw in text.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("- partition_index:") {
                flush()
                current = [:]
            } else if line.contains(":"), !line.hasPrefix("#") {
                let parts = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
                guard parts.count == 2 else { continue }
                let key = parts[0].trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "- "))
                    .lowercased()
                current[key] = parts[1].trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            }
        }
        flush()
        return listed
    }

    static func isOptionalPackageFile(_ relative: String) -> Bool {
        guard !relative.contains("/") else { return false }
        let name = relative.lowercased()
        return name == "updater.png" || name == "updater.jpg" || name == "updater.jpeg"
            || name == "manifest.json" || (name.hasSuffix(".txt") && name.contains("rockbox"))
            || name.hasSuffix(".auth")
    }

    private static func normalizedZipPath(_ raw: String) -> String? {
        var name = raw.replacingOccurrences(of: "\\", with: "/")
        let directory = name.hasSuffix("/")
        if name.hasPrefix("/") || name.range(of: #"^[A-Za-z]:"#, options: .regularExpression) != nil {
            return nil
        }
        var parts: [String] = []
        for part in name.split(separator: "/", omittingEmptySubsequences: true) {
            if part == "." { continue }
            if part == ".." { return nil }
            parts.append(String(part))
        }
        guard !parts.isEmpty else { return nil }
        name = parts.joined(separator: "/")
        return directory ? name + "/" : name
    }

    private static func extractZip(_ archive: URL, to destination: URL) async throws {
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        _ = try await NativeShell.require("/usr/bin/ditto", arguments: ["-x", "-k", archive.path, destination.path])
    }

    private static func extractEntry(zip: URL, entry: String, to destination: URL) async throws {
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        let result = try await NativeShell.run(
            "/usr/bin/unzip",
            arguments: ["-p", zip.path, entry],
            stdoutTo: destination
        )
        guard result.status == 0, FileManager.default.fileExists(atPath: destination.path) else {
            throw NativePackagingError.tool("unzip", result.status, "could not extract \(entry): \(result.stderr)")
        }
    }

    private static func isMacMetadata(_ name: String) -> Bool {
        let lower = name.lowercased()
        return lower == "__macosx" || lower == "_macosx"
    }

    private static func removeJunk(in root: URL) {
        let manager = FileManager.default
        for name in ["__MACOSX", "_MACOSX"] {
            try? manager.removeItem(at: root.appendingPathComponent(name))
        }
        guard let enumerator = manager.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey]) else {
            return
        }
        while let url = enumerator.nextObject() as? URL {
            if isMacMetadata(url.lastPathComponent) || url.lastPathComponent == ".DS_Store" {
                try? manager.removeItem(at: url)
            }
        }
    }

    private static func updateInfoPlist(_ url: URL) throws {
        guard let data = try? Data(contentsOf: url),
              var plist = try PropertyListSerialization.propertyList(from: data, options: [], format: nil)
                as? [String: Any] else { return }
        plist["CFBundleName"] = "Install Firmware"
        plist["CFBundleDisplayName"] = "Install Firmware"
        let output = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try output.write(to: url, options: .atomic)
    }

    private static func macosReadme(
        dmgName: String,
        firmwareFolder: String,
        firmwareAsset: String,
        software: String,
        version: String
    ) -> String {
        let title = [software, version].filter { !$0.isEmpty }.joined(separator: " ")
        return [
            "Innioasis Updater for Mac + Firmware Bundle",
            "==========================================",
            "",
            "Firmware: \(title.isEmpty ? software : title) (\(firmwareAsset))",
            "",
            "Quick start on macOS:",
            "1. Open \"\(dmgName)\" and drag \"Innioasis Updater\" to Applications.",
            "2. Extract this whole download and keep the \"\(firmwareFolder)\" folder",
            "   next to the DMG. It contains the firmware images and fw_package.ini.",
            "3. Launch Innioasis Updater, choose \"Install from file...\", select the",
            "   \"\(firmwareFolder)\" folder from this download (the original software",
            "   download's Firmware folder), then click \"Install\".",
            "4. If macOS prevents opening the app, open System Settings > Privacy & Security,",
            "   click \"Open Anyway\", then confirm.",
            "5. If this package includes an \"After Install Steps\" file, open it",
            "   after the install finishes.",
            "",
            "The firmware folder is the original software download's payload,",
            "repackaged beside the signed upstream DMG. The DMG is copied unchanged.",
            "",
        ].joined(separator: "\n")
    }
}
