import Foundation

struct GitHubRepository: Decodable, Identifiable, Sendable {
    let name: String
    let fullName: String
    let htmlURL: String
    let archived: Bool?
    let privateRepo: Bool?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case fullName = "full_name"
        case htmlURL = "html_url"
        case archived
        case privateRepo = "private"
    }
}

struct GitHubAsset: Decodable, Sendable {
    let id: Int?
    let name: String
    let size: Int?
    let digest: String?
    let browserDownloadURL: String?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case size
        case digest
        case browserDownloadURL = "browser_download_url"
    }
}

struct GitHubRelease: Decodable, Sendable {
    let id: Int
    let tagName: String
    let name: String?
    let draft: Bool?
    let prerelease: Bool?
    let assets: [GitHubAsset]
    let publishedAt: String?
    let body: String?
    let htmlURL: String?

    enum CodingKeys: String, CodingKey {
        case id
        case tagName = "tag_name"
        case name
        case draft
        case prerelease
        case assets
        case publishedAt = "published_at"
        case body
        case htmlURL = "html_url"
    }
}

struct GitHubFileDocument: Sendable {
    let text: String
    let sha: String?
}

struct MirrorPlatform: Sendable, Identifiable {
    let key: String
    let suffix: String
    let updaterAsset: String
    let layout: String
    let os: String
    let arch: String
    let optional: Bool
    let pinnedTag: String

    var id: String { key }

    var isDMGBundle: Bool { key == "macos" || layout == "beside_dmg" }

    var label: String {
        switch key {
        case "windows": return "Windows x86-64"
        case "windows-arm64": return "Windows arm64"
        case "linux": return "Linux x86-64"
        case "linux-arm64": return "Linux arm64"
        case "macos": return "macOS universal"
        default: return key
        }
    }
}

struct PlatformConfig: Decodable, Sendable {
    let suffix: String?
    let enabled: Bool?
    let updaterAsset: String?
    let layout: String?
    let os: String?
    let arch: String?
    let optional: Bool?
    let tag: String?

    enum CodingKeys: String, CodingKey {
        case suffix
        case enabled
        case updaterAsset = "updater_asset"
        case layout
        case os
        case arch
        case optional
        case tag
    }
}

struct FirmwareRules: Decodable, Sendable {
    let include: [String]?
    let exclude: [String]?
    let requireScatter: Bool?

    enum CodingKeys: String, CodingKey {
        case include
        case exclude
        case requireScatter = "require_scatter"
    }
}

struct FirmwareDefaults: Decodable, Sendable {
    let softwareName: String?
    let targetModel: String?
    let targetType: String?
    let description: String?

    enum CodingKeys: String, CodingKey {
        case softwareName = "software_name"
        case targetModel = "target_model"
        case targetType = "target_type"
        case description
    }
}

struct FirmwareRule: Decodable, Sendable {
    let matchAsset: String
    let softwareName: String?
    let targetModel: String?
    let targetType: String?
    let description: String?

    enum CodingKeys: String, CodingKey {
        case matchAsset = "match_asset"
        case softwareName = "software_name"
        case targetModel = "target_model"
        case targetType = "target_type"
        case description
    }

    func matches(_ name: String) -> Bool {
        guard let expression = try? NSRegularExpression(pattern: matchAsset, options: [.caseInsensitive]) else {
            return false
        }
        return expression.firstMatch(
            in: name,
            range: NSRange(name.startIndex..<name.endIndex, in: name)
        ) != nil
    }
}

struct ReleaseRules: Decodable, Sendable {
    let includePrereleases: Bool?
    let tags: [String]?
    let excludeTags: [String]?

    enum CodingKeys: String, CodingKey {
        case includePrereleases = "include_prereleases"
        case tags
        case excludeTags = "exclude_tags"
    }
}

struct MirrorConfig: Decodable, Sendable {
    let sourceRepo: String
    let mirrorRepo: String?
    let branch: String?
    let updaterRepo: String?
    let titlePrefix: String?
    let copyOriginalAssets: Bool?
    let maxAssetBytes: Int?
    let platforms: [String: PlatformConfig]?
    let firmware: FirmwareRules?
    let defaults: FirmwareDefaults?
    let rules: [FirmwareRule]?
    let releases: ReleaseRules?

    enum CodingKeys: String, CodingKey {
        case sourceRepo = "source_repo"
        case mirrorRepo = "mirror_repo"
        case branch
        case updaterRepo = "updater_repo"
        case titlePrefix = "title_prefix"
        case copyOriginalAssets = "copy_original_assets"
        case maxAssetBytes = "max_asset_bytes"
        case platforms
        case firmware
        case defaults
        case rules
        case releases
    }

    var effectiveBranch: String {
        let value = branch?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? "main" : value
    }

    var effectiveUpdaterRepo: String {
        let value = updaterRepo?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? "y1-community/updater-ce-neo" : value
    }

    var effectiveTitlePrefix: String {
        titlePrefix ?? "Portable Flashing Package: "
    }

    var enabledPlatforms: [MirrorPlatform] {
        guard let platforms else { return [] }
        return platforms
            .filter { $0.value.enabled != false }
            .compactMap { key, value in
                guard let suffix = value.suffix, !suffix.isEmpty,
                      let updaterAsset = value.updaterAsset, !updaterAsset.isEmpty else {
                    return nil
                }
                return MirrorPlatform(
                    key: key,
                    suffix: suffix,
                    updaterAsset: updaterAsset,
                    layout: value.layout ?? "sp_flash_tool",
                    os: value.os ?? "",
                    arch: value.arch ?? "",
                    optional: value.optional == true,
                    pinnedTag: value.tag ?? ""
                )
            }
            .sorted { platformOrder($0.key) < platformOrder($1.key) }
    }

    private func platformOrder(_ key: String) -> Int {
        switch key {
        case "windows": return 0
        case "windows-arm64": return 1
        case "linux": return 2
        case "linux-arm64": return 3
        case "macos": return 4
        default: return 10
        }
    }
}

struct Mirror: Identifiable, Sendable {
    let name: String
    let fullName: String
    var sourceRepo: String = ""
    var htmlURL: String
    var branch: String = "main"
    var platforms: [MirrorPlatform] = []
    var config: MirrorConfig?
    var latestSourceTag: String?
    var latestSourceName: String?
    var latestSourcePublishedAt: Date?
    var latestMirrorTag: String?
    var newTags: [String] = []
    var incompleteTags: [String] = []
    var lastChecked: Date?
    var lastSync: Date?
    var fetchError: String?
    var syncError: String?

    var id: String { name }

    var displayName: String {
        let trimmed = name.hasSuffix("-mirror") ? String(name.dropLast("-mirror".count)) : name
        return trimmed
            .replacingOccurrences(of: "_", with: "-")
            .split(separator: "-")
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: " ")
    }

    var sourceDisplayName: String {
        sourceRepo.isEmpty ? "Resolving source..." : sourceRepo
    }

    var mirrorURL: URL? {
        URL(string: htmlURL)
    }

    var sourceURL: URL? {
        guard !sourceRepo.isEmpty else { return nil }
        return URL(string: "https://github.com/\(sourceRepo)")
    }

    var sourceReleasesURL: URL? {
        sourceURL?.appendingPathComponent("releases")
    }

    var mirrorReleasesURL: URL? {
        mirrorURL?.appendingPathComponent("releases")
    }

    var needsAttention: Bool {
        fetchError != nil || syncError != nil || !newTags.isEmpty || !incompleteTags.isEmpty
    }
}

enum ActivityKind: Sendable {
    case info
    case success
    case warning
    case error
}

struct ActivityLine: Identifiable, Sendable {
    let id = UUID()
    let date: Date
    let text: String
    let kind: ActivityKind
}

struct GitHubAuth: Sendable {
    var hasToken: Bool
    var source: String
    var ghPath: String?
    var detail: String
}

struct SyncJob: Identifiable, Sendable {
    let id = UUID()
    let mirrorName: String
    let tags: [String]?
    let automatic: Bool

    var label: String {
        if let tags, let first = tags.first {
            return tags.count == 1 ? first : "\(first) +\(tags.count - 1)"
        }
        return "all missing"
    }
}

struct SyncProgress: Sendable {
    let mirrorName: String
    let label: String
    let startedAt: Date
    var line: String
    var stopping: Bool = false
}

struct NativeSyncReport: Sendable {
    var builtAssets: [String] = []
    var currentTags: [String] = []
    var failures: [String] = []
    var uploadedOriginalAssets: [String] = []
}

func parseGitHubDate(_ value: String?) -> Date? {
    guard let value, !value.isEmpty else { return nil }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) {
        return date
    }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)
}

func wildcardMatch(_ value: String, pattern: String) -> Bool {
    var regex = "^"
    for character in pattern.lowercased() {
        switch character {
        case "*":
            regex += ".*"
        case "?":
            regex += "."
        default:
            regex += NSRegularExpression.escapedPattern(for: String(character))
        }
    }
    regex += "$"
    return value.range(of: regex, options: [.regularExpression, .caseInsensitive]) != nil
}
