import Foundation

enum AppPaths {
    static func applicationSupport() throws -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let directory = base.appendingPathComponent("Mirror Watch", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    static func cache() throws -> URL {
        let directory = try applicationSupport().appendingPathComponent("portable-apps", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    static func work(mirror: String) throws -> URL {
        let root = try applicationSupport().appendingPathComponent("work", isDirectory: true)
        let directory = root.appendingPathComponent("\(mirror)-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    static func download(_ name: String) throws -> URL {
        let root = try applicationSupport().appendingPathComponent("downloads", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root.appendingPathComponent("\(UUID().uuidString)-\(name)")
    }
}

struct PortableAppSpec: Sendable {
    let key: String
    let label: String
    let assetName: String
    let repository: String

    init(
        key: String,
        label: String,
        assetName: String,
        repository: String = "y1-community/updater-ce-neo"
    ) {
        self.key = key
        self.label = label
        self.assetName = assetName
        self.repository = repository
    }
}

struct PortableAppAsset: Sendable, Identifiable {
    let platformKey: String
    let label: String
    let tag: String
    let assetName: String
    let size: Int
    let localURL: URL

    var id: String { platformKey }
    var sizeText: String {
        ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file)
    }
}

actor PortableAppStore {
    static let specs: [PortableAppSpec] = [
        PortableAppSpec(key: "windows", label: "Windows x86-64", assetName: "UpdaterCE-Windows-Portable.zip"),
        PortableAppSpec(key: "windows-arm64", label: "Windows arm64", assetName: "UpdaterCE-Windows-Portable-arm64.zip"),
        PortableAppSpec(key: "linux", label: "Linux x86-64", assetName: "UpdaterCE-Linux-Portable.zip"),
        PortableAppSpec(key: "linux-arm64", label: "Linux arm64", assetName: "UpdaterCE-Linux-Portable-arm64.zip"),
        PortableAppSpec(
            key: "macos",
            label: "macOS universal DMG",
            assetName: "Innioasis.Updater.for.Mac.dmg",
            repository: "y1-community/innioasis-updater"
        ),
    ]

    private let client: GitHubClient
    private let root: URL
    private var assets: [String: PortableAppAsset] = [:]
    private var refreshing = false

    init(client: GitHubClient) throws {
        self.client = client
        root = try AppPaths.cache()
    }

    func latest() -> [String: PortableAppAsset] {
        assets
    }

    func localURL(for platformKey: String) async -> URL? {
        if let asset = assets[platformKey],
           FileManager.default.fileExists(atPath: asset.localURL.path) {
            return asset.localURL
        }
        _ = try? await refresh { _ in }
        return assets[platformKey]?.localURL
    }

    func refresh(progress: @escaping @Sendable (String) -> Void) async throws -> [String: PortableAppAsset] {
        while refreshing {
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        refreshing = true
        defer { refreshing = false }

        var releasesByRepository: [String: [GitHubRelease]] = [:]
        for spec in Self.specs {
            let releases: [GitHubRelease]
            if let cached = releasesByRepository[spec.repository] {
                releases = cached
            } else {
                releases = try await client.releases(repository: spec.repository)
                releasesByRepository[spec.repository] = releases
            }
            guard let match = newestAsset(for: spec, releases: releases) else {
                progress("Portable app: \(spec.label) is not published yet")
                continue
            }
            let release = match.release
            let asset = match.asset
            let safeTag = release.tagName.replacingOccurrences(of: "/", with: "_")
            let platformRoot = root.appendingPathComponent(spec.key, isDirectory: true)
            let destination = platformRoot
                .appendingPathComponent(safeTag, isDirectory: true)
                .appendingPathComponent(asset.name)
            let expected = asset.size ?? 0
            let existing = (try? FileManager.default.attributesOfItem(atPath: destination.path)[.size] as? Int) ?? 0
            if existing == expected, expected > 0 {
                assets[spec.key] = PortableAppAsset(
                    platformKey: spec.key,
                    label: spec.label,
                    tag: release.tagName,
                    assetName: asset.name,
                    size: existing,
                    localURL: destination
                )
                progress("Portable app ready: \(spec.label) \(release.tagName)")
                continue
            }

            do {
                try FileManager.default.createDirectory(
                    at: destination.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                let partial = destination.appendingPathExtension("part")
                if FileManager.default.fileExists(atPath: partial.path) {
                    try FileManager.default.removeItem(at: partial)
                }
                progress("Downloading portable app: \(spec.label) \(release.tagName) (\(asset.name))")
                try await client.downloadAsset(repository: spec.repository, asset: asset, to: partial)
                if FileManager.default.fileExists(atPath: destination.path) {
                    try FileManager.default.removeItem(at: destination)
                }
                try FileManager.default.moveItem(at: partial, to: destination)
                let size = (try? FileManager.default.attributesOfItem(atPath: destination.path)[.size] as? Int) ?? expected
                assets[spec.key] = PortableAppAsset(
                    platformKey: spec.key,
                    label: spec.label,
                    tag: release.tagName,
                    assetName: asset.name,
                    size: size,
                    localURL: destination
                )
                progress("Stored portable app: \(spec.label) \(release.tagName) (\(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file)))")
                if let entries = try? FileManager.default.contentsOfDirectory(atPath: platformRoot.path) {
                    for entry in entries where entry != safeTag {
                        try? FileManager.default.removeItem(at: platformRoot.appendingPathComponent(entry))
                    }
                }
            } catch {
                progress("Portable app download failed for \(spec.label): \(error.localizedDescription)")
            }
        }
        return assets
    }

    private func newestAsset(
        for spec: PortableAppSpec,
        releases: [GitHubRelease]
    ) -> (release: GitHubRelease, asset: GitHubAsset)? {
        for release in releases where release.draft != true {
            if let asset = release.assets.first(where: {
                $0.name.caseInsensitiveCompare(spec.assetName) == .orderedSame
            }) {
                return (release, asset)
            }
        }
        return nil
    }
}
