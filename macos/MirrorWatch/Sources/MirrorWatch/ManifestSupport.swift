import Foundation

struct ManifestPackage: Sendable {
    let name: String
    let repo: String
    let device: String
    let postInstall: String
    let derivative: String
}

struct ManifestProduct: Sendable {
    let sourceRepo: String
    let mirrorRepo: String
    let packages: [ManifestPackage]

    var names: [String] {
        var seen: [String] = []
        for package in packages where !package.name.isEmpty && !seen.contains(package.name) {
            seen.append(package.name)
        }
        return seen
    }

    var devices: [String] {
        var seen: [String] = []
        for package in packages {
            let device = package.device.trimmingCharacters(in: .whitespaces).uppercased()
            if !device.isEmpty && !seen.contains(device) { seen.append(device) }
        }
        return seen
    }

    var softwareName: String {
        if names.count == 1 { return names[0] }
        return sourceRepo.split(separator: "/").last.map {
            String($0).replacingOccurrences(of: "-", with: " ").capitalized
        } ?? "Firmware"
    }
}

enum ManifestError: LocalizedError {
    case invalidXML(String)
    case download(String)

    var errorDescription: String? {
        switch self {
        case let .invalidXML(message): return "slidia_manifest.xml is invalid: \(message)"
        case let .download(message): return "Could not download slidia_manifest.xml: \(message)"
        }
    }
}

final class ManifestParser: NSObject, XMLParserDelegate {
    private var packages: [ManifestPackage] = []
    private var parseError: String?

    static func parse(_ data: Data) throws -> [ManifestPackage] {
        let parser = XMLParser(data: data)
        let delegate = ManifestParser()
        parser.delegate = delegate
        guard parser.parse() else {
            throw ManifestError.invalidXML(delegate.parseError ?? parser.parserError?.localizedDescription ?? "parse failed")
        }
        return delegate.packages
    }

    func parser(
        _ parser: XMLParser,
        didStartElement elementName: String,
        namespaceURI: String?,
        qualifiedName qName: String?,
        attributes: [String: String]
    ) {
        guard elementName == "package" else { return }
        guard (attributes["type"] ?? "").lowercased() == "img" else { return }
        let repo = (attributes["repo"] ?? "").trimmingCharacters(in: .whitespaces)
        guard repo.range(of: #"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"#, options: .regularExpression) != nil else {
            return
        }
        packages.append(
            ManifestPackage(
                name: (attributes["name"] ?? "").trimmingCharacters(in: .whitespaces),
                repo: repo,
                device: (attributes["device"] ?? "").trimmingCharacters(in: .whitespaces),
                postInstall: (attributes["post_install"] ?? "").trimmingCharacters(in: .whitespaces),
                derivative: (attributes["derivative"] ?? "").trimmingCharacters(in: .whitespaces)
            )
        )
    }

    func parser(_ parser: XMLParser, parseErrorOccurred parseError: Error) {
        self.parseError = parseError.localizedDescription
    }
}

enum ManifestService {
    static let defaultURL = "https://innioasis.app/slidia_manifest.xml"

    static func fetch(url: String, session: URLSession = .shared) async throws -> Data {
        guard let requestURL = URL(string: url) else {
            throw ManifestError.download("invalid URL")
        }
        var request = URLRequest(url: requestURL)
        request.setValue("MirrorWatch/0.2", forHTTPHeaderField: "User-Agent")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw ManifestError.download("HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)")
        }
        return data
    }

    static func products(from data: Data, organization: String) throws -> [ManifestProduct] {
        let packages = try ManifestParser.parse(data)
        var grouped: [String: [ManifestPackage]] = [:]
        var order: [String] = []
        for package in packages {
            let key = package.repo.lowercased()
            if grouped[key] == nil {
                grouped[key] = []
                order.append(key)
            }
            grouped[key]?.append(package)
        }
        var used = Set<String>()
        return order.compactMap { key in
            guard let rows = grouped[key], let first = rows.first else { return nil }
            let mirror = "\(organization)/\(mirrorName(sourceRepo: first.repo, used: &used))"
            return ManifestProduct(sourceRepo: first.repo, mirrorRepo: mirror, packages: rows)
        }
    }

    static func mirrorName(sourceRepo: String, used: inout Set<String>) -> String {
        let pieces = sourceRepo.split(separator: "/", maxSplits: 1).map(String.init)
        let owner = pieces.first ?? "source"
        let raw = pieces.count > 1 ? pieces[1] : owner
        var slug = raw.lowercased().replacingOccurrences(
            of: #"[^a-z0-9._-]+"#,
            with: "-",
            options: .regularExpression
        )
        slug = slug.trimmingCharacters(in: CharacterSet(charactersIn: "-._"))
        if slug.isEmpty { slug = "firmware" }
        var candidate = slug.hasSuffix("-mirror") ? slug : slug + "-mirror"
        if used.contains(candidate) {
            candidate = "\(owner.lowercased())-\(candidate)".trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        }
        let original = candidate
        var counter = 2
        while used.contains(candidate) {
            candidate = "\(original)-\(counter)"
            counter += 1
        }
        used.insert(candidate)
        return candidate
    }

    static func generatedConfig(_ product: ManifestProduct, organization: String) -> String {
        let devices = product.devices.isEmpty ? ["Y1"] : product.devices
        var model = devices.contains("Y1") ? "Y1" : devices[0]
        if !["Y1", "Y2", "Generic MTK"].contains(model) { model = "Y1" }
        let defaultType = model == "Y2" ? "Stock" : "Type A"
        let rules: [[String: String]] = [
            ["match_asset": "(?i)360p", "target_type": "360p"],
            ["match_asset": "(?i)240p", "target_type": "240p"],
            ["match_asset": "(?i)rom_y1_type[-_ ]?a", "target_model": "Y1", "target_type": "Type A"],
            ["match_asset": "(?i)rom_y1_type[-_ ]?b", "target_model": "Y1", "target_type": "Type B"],
            ["match_asset": "(?i)type[-_ ]?b", "target_model": model, "target_type": "Type B"],
            ["match_asset": "(?i)(^|[-_.])y2([-_.]|$)", "target_model": "Y2", "target_type": "Stock"],
        ]
        let platforms: [String: Any] = [
            "windows": ["updater_asset": "UpdaterCE-Windows-Portable.zip", "suffix": "-windows.zip", "layout": "sp_flash_tool", "os": "windows", "arch": "x86-64"],
            "windows-arm64": ["updater_asset": "UpdaterCE-Windows-Portable-arm64.zip", "suffix": "-windows-arm64.zip", "layout": "sp_flash_tool", "os": "windows", "arch": "arm64", "optional": true],
            "linux": ["updater_asset": "UpdaterCE-Linux-Portable.zip", "suffix": "-linux.zip", "layout": "sp_flash_tool", "os": "linux", "arch": "x86-64"],
            "linux-arm64": ["updater_asset": "UpdaterCE-Linux-Portable-arm64.zip", "suffix": "-linux-arm64.zip", "layout": "sp_flash_tool", "os": "linux", "arch": "arm64", "optional": true],
            "macos": ["updater_asset": "Innioasis.Updater.for.Mac.dmg", "suffix": "-macos.zip", "layout": "beside_dmg", "os": "macos", "arch": "universal", "updater_repo": "y1-community/innioasis-updater"],
        ]
        let config: [String: Any] = [
            "source_repo": product.sourceRepo,
            "mirror_repo": product.mirrorRepo,
            "branch": "main",
            "updater_repo": "y1-community/updater-ce-neo",
            "title_prefix": "Portable Flashing Package: ",
            "copy_original_assets": true,
            "source_readme": [
                "enabled": true,
                "path": "README.md",
                "commit_message": "Mirror source README",
            ],
            "build_workers": 2,
            "max_asset_bytes": 2_147_483_648,
            "releases": ["include_prereleases": true, "tags": [], "exclude_tags": []],
            "defaults": [
                "software_name": product.softwareName,
                "target_model": model,
                "target_type": defaultType,
                "description": "",
            ],
            "rules": rules,
            "firmware": [
                "include": ["*.zip"],
                "exclude": [
                    "*-windows.zip", "*-windows-arm64.zip", "*-linux.zip", "*-linux-arm64.zip",
                    "*-macos.zip", "*portable*.zip", "*source*.zip", "*installer*.zip", "*.dmg",
                    "*.apk", "*.tar.gz", "*.tgz", "*.tar", "*.sha256", "*.json", "*.txt",
                    "*.sig", "*.img.gz", "*.exe", "*.appimage",
                ],
                "require_scatter": true,
            ],
            "platforms": platforms,
        ]
        let data = (try? JSONSerialization.data(
            withJSONObject: config,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        )) ?? Data()
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    static func upgradedLegacyConfig(_ text: String, generated: String) -> String? {
        guard var existing = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any],
              let defaults = (try? JSONSerialization.jsonObject(with: Data(generated.utf8))) as? [String: Any] else {
            return nil
        }
        var changed = false

        if let generatedPlatforms = defaults["platforms"] as? [String: Any] {
            var platforms = existing["platforms"] as? [String: Any] ?? [:]
            var platformsChanged = false
            for (key, value) in generatedPlatforms where platforms[key] == nil {
                platforms[key] = value
                platformsChanged = true
            }
            if let desired = generatedPlatforms["macos"] as? [String: Any],
               var macos = platforms["macos"] as? [String: Any] {
                for field in ["updater_asset", "layout", "updater_repo"] {
                    guard let value = desired[field] as? String,
                          macos[field] as? String != value else { continue }
                    macos[field] = value
                    platformsChanged = true
                }
                platforms["macos"] = macos
            }
            if platformsChanged {
                existing["platforms"] = platforms
                changed = true
            }
        }
        if existing["firmware"] == nil, let firmware = defaults["firmware"] {
            existing["firmware"] = firmware
            changed = true
        }
        guard changed else { return nil }
        guard let data = try? JSONSerialization.data(
            withJSONObject: existing,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        ) else {
            return nil
        }
        return String(data: data, encoding: .utf8).map { $0 + "\n" }
    }

    static func ensureMirrors(
        client: GitHubClient,
        organization: String,
        manifestData: Data,
        progress: @escaping @Sendable (String) -> Void
    ) async throws -> [ManifestProduct] {
        let products = try products(from: manifestData, organization: organization)
        for product in products {
            guard let source = try await client.repository(product.sourceRepo) else {
                progress("Manifest: skipping missing source \(product.sourceRepo)")
                continue
            }
            _ = source
            let existing = try await client.repository(product.mirrorRepo)
            if existing == nil {
                let name = product.mirrorRepo.split(separator: "/").last.map(String.init) ?? product.mirrorRepo
                let description = "Portable flashing packages for \(product.sourceRepo)"
                do {
                    _ = try await client.createRepository(
                        organization: organization,
                        name: name,
                        description: description
                    )
                    progress("Created \(product.mirrorRepo)")
                    try await Task.sleep(nanoseconds: 900_000_000)
                } catch {
                    progress("Could not create \(product.mirrorRepo): \(error.localizedDescription)")
                    continue
                }
            }
            let config = generatedConfig(product, organization: organization)
            var document: GitHubFileDocument?
            do {
                document = try await client.fileDocument(
                    repository: product.mirrorRepo,
                    path: "mirror.config.json",
                    ref: nil,
                    fresh: true
                )
            } catch let GitHubClientError.http(status, _) where status == 404 {
                document = nil
            } catch {
                progress("Could not read config for \(product.mirrorRepo): \(error.localizedDescription)")
                continue
            }

            do {
                if let document {
                    guard let upgraded = upgradedLegacyConfig(document.text, generated: config) else {
                        continue
                    }
                    try await client.putFile(
                        repository: product.mirrorRepo,
                        path: "mirror.config.json",
                        text: upgraded,
                        message: "Enable portable package platforms",
                        branch: "main",
                        sha: document.sha
                    )
                    progress("Upgraded config for \(product.mirrorRepo)")
                } else {
                    try await client.putFile(
                        repository: product.mirrorRepo,
                        path: "mirror.config.json",
                        text: config,
                        message: "Configure portable firmware packages",
                        branch: "main",
                        sha: nil
                    )
                    progress("Configured \(product.mirrorRepo)")
                }
            } catch {
                progress("Could not configure \(product.mirrorRepo): \(error.localizedDescription)")
            }
        }
        return products
    }
}
