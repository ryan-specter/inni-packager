import Foundation

private struct BuiltAsset {
    let name: String
    let sourceAsset: String
    let platform: String
    let size: Int
    let sha256: String
}

private struct IndexRelease {
    let source: GitHubRelease
    let title: String
    let assets: [BuiltAsset]
    let originalAssets: [String]
}

private struct ReleaseOutcome {
    let index: IndexRelease?
    let current: Bool
    let built: [String]
    let failure: String?
}

actor NativeSyncEngine {
    private let client: GitHubClient
    private let apps: PortableAppStore
    private let progress: @Sendable (String) -> Void
    private var cancelled = false

    init(
        client: GitHubClient,
        apps: PortableAppStore,
        progress: @escaping @Sendable (String) -> Void
    ) {
        self.client = client
        self.apps = apps
        self.progress = progress
    }

    func cancel() {
        cancelled = true
    }

    func sync(mirror: Mirror, tags: [String]?, forceMacos: Bool = false) async throws -> NativeSyncReport {
        var report = NativeSyncReport()
        guard let config = mirror.config else {
            throw NSError(
                domain: "MirrorWatch",
                code: 10,
                userInfo: [NSLocalizedDescriptionKey: "\(mirror.name) has no readable mirror.config.json."]
            )
        }
        let fullSelection = tags == nil || tags?.isEmpty == true
        let work = try AppPaths.work(mirror: mirror.name)
        defer { try? FileManager.default.removeItem(at: work) }

        let cachedApps = try await apps.refresh { [progress] line in progress(line) }
        let requiredPlatforms: [MirrorPlatform]
        if forceMacos {
            requiredPlatforms = config.enabledPlatforms.filter { $0.key == "macos" }
            guard !requiredPlatforms.isEmpty else {
                throw NSError(
                    domain: "MirrorWatch",
                    code: 13,
                    userInfo: [NSLocalizedDescriptionKey: "macOS is not enabled for \(mirror.name)."]
                )
            }
        } else {
            requiredPlatforms = config.enabledPlatforms.filter { !$0.optional }
        }
        let missingRequired = requiredPlatforms.filter { cachedApps[$0.key] == nil }
        guard missingRequired.isEmpty else {
            throw NSError(
                domain: "MirrorWatch",
                code: 12,
                userInfo: [NSLocalizedDescriptionKey:
                    "Required portable app missing: \(missingRequired.map(\.label).joined(separator: ", "))."]
            )
        }
        let platforms = config.enabledPlatforms.filter { cachedApps[$0.key] != nil }
        if platforms.isEmpty {
            throw NSError(
                domain: "MirrorWatch",
                code: 11,
                userInfo: [NSLocalizedDescriptionKey: "No portable app is available for \(mirror.name)."]
            )
        }

        let sourceReleases = try await client.releases(repository: config.sourceRepo)
        var selected = sourceReleases.filter { release in
            guard release.draft != true, releaseHasFirmware(release, config: config) else { return false }
            if release.prerelease == true, config.releases?.includePrereleases == false { return false }
            let tag = release.tagName
            if let allowed = config.releases?.tags, !allowed.isEmpty,
               !allowed.contains(where: { wildcardMatch(tag, pattern: $0) }) {
                return false
            }
            if let excluded = config.releases?.excludeTags,
               excluded.contains(where: { wildcardMatch(tag, pattern: $0) }) {
                return false
            }
            return true
        }
        if let tags, !tags.isEmpty {
            selected = selected.filter { tags.contains($0.tagName) }
        }
        if selected.isEmpty {
            progress("\(mirror.name): nothing new to build.")
            return report
        }

        var touched: [IndexRelease] = []
        for release in selected {
            try checkCancelled()
            do {
                let outcome = try await syncRelease(
                    mirror: mirror,
                    config: config,
                    source: release,
                    platforms: platforms,
                    apps: cachedApps,
                    forceMacos: forceMacos,
                    work: work
                )
                if let index = outcome.index { touched.append(index) }
                if outcome.current { report.currentTags.append(release.tagName) }
                report.builtAssets.append(contentsOf: outcome.built)
                if let failure = outcome.failure {
                    report.failures.append("\(release.tagName): \(failure)")
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                report.failures.append("\(release.tagName): \(error.localizedDescription)")
                progress("\(release.tagName): \(error.localizedDescription)")
            }
        }

        if !touched.isEmpty {
            do {
                try await updateIndex(
                    mirror: mirror,
                    config: config,
                    releases: touched,
                    fullSelection: fullSelection
                )
            } catch {
                report.failures.append("index.json: \(error.localizedDescription)")
                progress("Index update failed: \(error.localizedDescription)")
            }
        }
        return report
    }

    private func syncRelease(
        mirror: Mirror,
        config: MirrorConfig,
        source: GitHubRelease,
        platforms: [MirrorPlatform],
        apps: [String: PortableAppAsset],
        forceMacos: Bool,
        work: URL
    ) async throws -> ReleaseOutcome {
        let firmware = firmwareAssets(in: source, config: config)
        guard !firmware.isEmpty else {
            return ReleaseOutcome(index: nil, current: false, built: [], failure: "no firmware zip")
        }
        let releaseWork = work.appendingPathComponent("release-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: releaseWork, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: releaseWork) }
        let existing = try await client.release(repository: mirror.fullName, tag: source.tagName)
        let packageVersion = NativePackager.releaseVersion(source.tagName, title: source.name)
        var existingAssets = Dictionary(
            uniqueKeysWithValues: (existing?.assets ?? []).map { ($0.name, $0) }
        )
        let expected: Set<String>
        if let block = blockAssetNames(from: existing?.body) {
            expected = block
        } else {
            expected = Set(firmware.flatMap { asset in
                platforms.map { outputName(asset.name, suffix: $0.suffix) }
            })
        }
        let macPlatform = platforms.first { $0.key == "macos" }
        let macSourceNames: Set<String>
        if forceMacos, let macPlatform {
            var macNames = expected
            macNames.formUnion(existingAssets.keys)
            macSourceNames = Set(macNames.compactMap { name -> String? in
                guard name.hasSuffix(macPlatform.suffix) else { return nil }
                return String(name.dropLast(macPlatform.suffix.count)) + ".zip"
            })
        } else {
            macSourceNames = []
        }
        let forceMac = forceMacos && macPlatform != nil
        if forceMac, macSourceNames.isEmpty {
            progress("\(source.tagName): no macOS packages to replace")
            return ReleaseOutcome(index: nil, current: true, built: [], failure: nil)
        }
        let maxBytes = config.maxAssetBytes ?? 2_147_483_648
        let originals = config.copyOriginalAssets == false ? [] : source.assets.filter {
            $0.name.lowercased() != "stats.txt" && ($0.size ?? 0) <= maxBytes
        }
        let packagesCurrent = !forceMac && expected.allSatisfy({ (existingAssets[$0]?.size ?? 0) > 0 })
        let originalsCurrent = originals.allSatisfy {
            (existingAssets[$0.name]?.size ?? -1) == ($0.size ?? -2)
        }
        if existing != nil, packagesCurrent, originalsCurrent {
            progress("\(source.tagName): already current")
            return ReleaseOutcome(index: nil, current: true, built: [], failure: nil)
        }

        var firmwareFiles: [String: URL] = [:]
        var valid: [(GitHubAsset, FirmwareArchive, ResolvedFirmwareMetadata, String)] = []
        for asset in firmware {
            let needs: Bool
            if forceMac {
                needs = macSourceNames.contains(asset.name)
            } else {
                needs = platforms.contains { platform in
                    (existingAssets[outputName(asset.name, suffix: platform.suffix)]?.size ?? 0) <= 0
                }
            }
            guard needs else { continue }
            try checkCancelled()
            progress("\(source.tagName): downloading \(asset.name)")
            let destination = releaseWork.appendingPathComponent("source", isDirectory: true)
                .appendingPathComponent(UUID().uuidString + "-" + asset.name)
            try await client.downloadAsset(repository: config.sourceRepo, asset: asset, to: destination)
            firmwareFiles[asset.name] = destination
            do {
                let archive = try await NativePackager.inspectFirmware(destination)
                guard archive.hasScatter else {
                    progress("\(source.tagName): skipping \(asset.name), no scatter")
                    continue
                }
                let metadata = NativePackager.resolveMetadata(
                    config: config,
                    assetName: asset.name,
                    releaseName: source.name ?? source.tagName
                )
                let ini = NativePackager.fwPackageIni(metadata, version: packageVersion)
                valid.append((asset, archive, metadata, ini))
            } catch {
                progress("\(source.tagName): \(asset.name): \(error.localizedDescription)")
            }
        }
        let candidates: [GitHubAsset]
        if forceMac {
            candidates = firmware.filter { macSourceNames.contains($0.name) }
        } else {
            candidates = firmware.filter { asset in
                platforms.contains { (existingAssets[outputName(asset.name, suffix: $0.suffix)]?.size ?? 0) <= 0 }
            }
        }
        if valid.isEmpty, !candidates.isEmpty {
            return ReleaseOutcome(
                index: nil,
                current: false,
                built: [],
                failure: "no flashable firmware zip"
            )
        }

        var release = existing
        if release == nil {
            let title = releaseTitle(config: config, source: source)
            progress("\(source.tagName): creating mirror release")
            release = try await client.createRelease(
                repository: mirror.fullName,
                tag: source.tagName,
                title: title,
                body: source.body ?? "",
                prerelease: source.prerelease == true
            )
            progress("\(source.tagName): created release")
        }
        guard let release else {
            return ReleaseOutcome(index: nil, current: false, built: [], failure: "release creation failed")
        }

        var built: [BuiltAsset] = []
        var buildFailures: [String] = []
        var originalNames: [String] = []
        for (asset, archive, metadata, ini) in valid {
            for platform in platforms {
                try checkCancelled()
                if forceMac, platform.key != "macos" { continue }
                let name = outputName(asset.name, suffix: platform.suffix)
                if !forceMac, (existingAssets[name]?.size ?? 0) > 0 { continue }
                guard let app = apps[platform.key] else {
                    if !platform.optional {
                        progress("\(source.tagName): \(platform.label) portable app is missing")
                    }
                    continue
                }
                let output = releaseWork.appendingPathComponent("build", isDirectory: true)
                    .appendingPathComponent(UUID().uuidString + "-" + name)
                progress("\(source.tagName): packing \(asset.name) for \(platform.key)")
                do {
                    let result = try await NativePackager.buildPackage(
                        portableZip: app.localURL,
                        firmware: archive,
                        platform: platform,
                        output: output,
                        iniText: ini,
                        softwareName: metadata.softwareName,
                        version: packageVersion,
                        sourceAsset: asset.name,
                        sourceRepo: config.sourceRepo
                    )
                    if result.size > maxBytes {
                        throw NativePackagingError.emptyPackage("\(name) is over GitHub's asset limit")
                    }
                    if let duplicate = existingAssets[name], let id = duplicate.id {
                        try await client.deleteAsset(repository: mirror.fullName, assetID: id)
                    }
                    _ = try await client.uploadAsset(
                        repository: mirror.fullName,
                        releaseID: release.id,
                        fileURL: result.url,
                        name: name
                    )
                    let uploaded = GitHubAsset(
                        id: nil,
                        name: name,
                        size: result.size,
                        digest: "sha256:\(result.sha256)",
                        browserDownloadURL: nil
                    )
                    existingAssets[name] = uploaded
                    built.append(
                        BuiltAsset(
                            name: name,
                            sourceAsset: asset.name,
                            platform: platform.key,
                            size: result.size,
                            sha256: result.sha256
                        )
                    )
                    progress("\(source.tagName): uploaded \(name) (\(ByteCountFormatter.string(fromByteCount: Int64(result.size), countStyle: .file)))")
                    try? FileManager.default.removeItem(at: output)
                } catch is CancellationError {
                    throw CancellationError()
                } catch {
                    let message = "\(name): \(error.localizedDescription)"
                    buildFailures.append(message)
                    progress("\(source.tagName): \(message)")
                }
            }
        }

        if config.copyOriginalAssets != false {
            for asset in source.assets {
                guard asset.name.lowercased() != "stats.txt" else { continue }
                guard (asset.size ?? 0) <= maxBytes else {
                    progress("\(source.tagName): skipping oversized original \(asset.name)")
                    continue
                }
                if (existingAssets[asset.name]?.size ?? 0) == (asset.size ?? -1) {
                    originalNames.append(asset.name)
                    continue
                }
                try checkCancelled()
                do {
                    let local = firmwareFiles[asset.name] ?? releaseWork.appendingPathComponent("original", isDirectory: true)
                        .appendingPathComponent(UUID().uuidString + "-" + asset.name)
                    if !FileManager.default.fileExists(atPath: local.path) {
                        progress("\(source.tagName): copying original \(asset.name)")
                        try await client.downloadAsset(repository: config.sourceRepo, asset: asset, to: local)
                    }
                    if let duplicate = existingAssets[asset.name], let id = duplicate.id {
                        try await client.deleteAsset(repository: mirror.fullName, assetID: id)
                    }
                    _ = try await client.uploadAsset(
                        repository: mirror.fullName,
                        releaseID: release.id,
                        fileURL: local,
                        name: asset.name
                    )
                    existingAssets[asset.name] = asset
                    originalNames.append(asset.name)
                    progress("\(source.tagName): uploaded original \(asset.name)")
                } catch is CancellationError {
                    throw CancellationError()
                } catch {
                    progress("\(source.tagName): original \(asset.name) failed: \(error.localizedDescription)")
                }
            }
        }

        var indexAssets: [BuiltAsset] = []
        let byName = Dictionary(uniqueKeysWithValues: built.map { ($0.name, $0) })
        for asset in existingAssets.values {
            guard let platform = platforms.first(where: { asset.name.hasSuffix($0.suffix) }) else { continue }
            let sourceName = String(asset.name.dropLast(platform.suffix.count)) + ".zip"
            if let built = byName[asset.name] {
                indexAssets.append(built)
            } else {
                indexAssets.append(
                    BuiltAsset(
                        name: asset.name,
                        sourceAsset: sourceName,
                        platform: platform.key,
                        size: asset.size ?? 0,
                        sha256: (asset.digest ?? "").replacingOccurrences(of: "sha256:", with: "")
                    )
                )
            }
        }
        return ReleaseOutcome(
            index: IndexRelease(
                source: source,
                title: releaseTitle(config: config, source: source),
                assets: indexAssets,
                originalAssets: originalNames
            ),
            current: false,
            built: built.map(\.name),
            failure: buildFailures.isEmpty ? nil : buildFailures.joined(separator: "; ")
        )
    }

    private func updateIndex(
        mirror: Mirror,
        config: MirrorConfig,
        releases: [IndexRelease],
        fullSelection: Bool
    ) async throws {
        var document: GitHubFileDocument?
        do {
            document = try await client.fileDocument(
                repository: mirror.fullName,
                path: "index.json",
                ref: config.effectiveBranch,
                fresh: true
            )
        } catch let GitHubClientError.http(status, _) where status == 404 {
            document = nil
        }
        if document == nil && !fullSelection {
            progress("index.json is missing; selective sync leaves it untouched.")
            return
        }

        var root: [String: Any]
        if let text = document?.text,
           let data = text.data(using: .utf8),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            root = existing
        } else {
            root = [
                "schema": 1,
                "source_repo": config.sourceRepo,
                "mirror_repo": mirror.fullName,
                "title_prefix": config.effectiveTitlePrefix,
                "updater_repo": config.effectiveUpdaterRepo,
            ]
        }
        root["generated_at"] = ISO8601DateFormatter().string(from: Date())
        root["source_repo"] = config.sourceRepo
        root["mirror_repo"] = mirror.fullName
        root["title_prefix"] = config.effectiveTitlePrefix
        root["updater_repo"] = config.effectiveUpdaterRepo
        root["platforms"] = config.enabledPlatforms.map { platform in
            [
                "key": platform.key,
                "os": platform.os,
                "arch": platform.arch,
                "suffix": platform.suffix,
                "layout": platform.layout,
            ]
        }

        var entries = (root["releases"] as? [[String: Any]]) ?? []
        for release in releases {
            let tag = release.source.tagName
            let tagEscaped = tag.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? tag
            let assets: [[String: Any]] = release.assets.map { asset in
                let escaped = asset.name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? asset.name
                return [
                    "name": asset.name,
                    "source_asset": asset.sourceAsset,
                    "platform": asset.platform,
                    "size": asset.size,
                    "sha256": asset.sha256,
                    "url": "https://github.com/\(mirror.fullName)/releases/download/\(tagEscaped)/\(escaped)",
                ]
            }
            let entry: [String: Any] = [
                "tag": tag,
                "title": release.title,
                "source_title": release.source.name ?? tag,
                "source_url": "https://github.com/\(config.sourceRepo)/releases/tag/\(tagEscaped)",
                "published_at": release.source.publishedAt ?? "",
                "prerelease": release.source.prerelease == true,
                "source_notes": release.source.body ?? "",
                "assets": assets,
                "original_assets": release.originalAssets,
            ]
            entries.removeAll { ($0["tag"] as? String) == tag }
            entries.append(entry)
        }
        entries.sort {
            ($0["published_at"] as? String ?? "") > ($1["published_at"] as? String ?? "")
        }
        root["releases"] = entries
        let data = try JSONSerialization.data(
            withJSONObject: root,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        )
        let text = String(decoding: data, as: UTF8.self) + "\n"
        try await client.putFile(
            repository: mirror.fullName,
            path: "index.json",
            text: text,
            message: "Update portable package index",
            branch: config.effectiveBranch,
            sha: document?.sha
        )
        progress("Updated index.json for \(mirror.name)")
    }

    private func outputName(_ asset: String, suffix: String) -> String {
        let base = asset.lowercased().hasSuffix(".zip") ? String(asset.dropLast(4)) : asset
        return base + suffix
    }

    private func releaseTitle(config: MirrorConfig, source: GitHubRelease) -> String {
        let base = (source.name?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
            ? source.name!
            : source.tagName
        let prefix = config.effectiveTitlePrefix
        if !prefix.isEmpty, !base.lowercased().hasPrefix(prefix.lowercased()) {
            return prefix + base
        }
        return base
    }

    private func checkCancelled() throws {
        if cancelled { throw CancellationError() }
        try Task.checkCancellation()
    }
}
