import Foundation

enum GitHubClientError: LocalizedError {
    case notAuthenticated
    case badResponse
    case http(Int, String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .notAuthenticated:
            return "GitHub is not authenticated. Add a token in Settings."
        case .badResponse:
            return "GitHub returned an unreadable response."
        case let .http(status, message):
            return "GitHub API \(status): \(message)"
        case let .decoding(message):
            return "Could not read GitHub data: \(message)"
        }
    }
}

private struct CacheEntry {
    let etag: String
    let data: Data
}

enum LocalCLI {
    static let searchPaths = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]

    static func find(_ name: String) -> String? {
        var paths = (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":")
            .map(String.init)
        paths.append(contentsOf: searchPaths)
        var seen = Set<String>()
        for directory in paths where seen.insert(directory).inserted {
            let candidate = URL(fileURLWithPath: directory).appendingPathComponent(name).path
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }

    static func run(_ executable: String, arguments: [String]) -> (status: Int32, output: String)? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        process.environment = ProcessInfo.processInfo.environment
        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return (process.terminationStatus, String(decoding: data, as: UTF8.self))
        } catch {
            return nil
        }
    }
}

actor GitHubClient {
    private let session: URLSession
    private var cache: [String: CacheEntry] = [:]
    private var token: String?
    private var ghPath: String?
    private var prepared = false
    private(set) var auth = GitHubAuth(
        hasToken: false,
        source: "none",
        ghPath: nil,
        detail: "Not checked yet"
    )

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 7_200
        configuration.httpMaximumConnectionsPerHost = 6
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: configuration)
    }

    // MARK: - Authentication

    func prepare() async {
        guard !prepared else { return }
        prepared = true
        ghPath = LocalCLI.find("gh")

        if let saved = TokenStore.read() {
            token = saved
            auth = GitHubAuth(
                hasToken: true,
                source: "Keychain",
                ghPath: ghPath,
                detail: "Direct GitHub API access using the token saved in Keychain"
            )
            return
        }

        let environment = ProcessInfo.processInfo.environment
        if let value = environment["GH_TOKEN"] ?? environment["GITHUB_TOKEN"],
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            token = value.trimmingCharacters(in: .whitespacesAndNewlines)
            auth = GitHubAuth(
                hasToken: true,
                source: "GH_TOKEN",
                ghPath: ghPath,
                detail: "Direct GitHub API access using GH_TOKEN from the environment"
            )
            return
        }

        guard let ghPath else {
            auth = GitHubAuth(
                hasToken: false,
                source: "none",
                ghPath: nil,
                detail: "Add a GitHub token in Settings to read and publish releases."
            )
            return
        }

        guard let result = LocalCLI.run(ghPath, arguments: ["auth", "token"]),
              result.status == 0 else {
            auth = GitHubAuth(
                hasToken: false,
                source: "gh",
                ghPath: ghPath,
                detail: "No token saved. Add one in Settings, or run `gh auth login`."
            )
            return
        }

        let value = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            auth = GitHubAuth(
                hasToken: false,
                source: "gh",
                ghPath: ghPath,
                detail: "No token saved. Add one in Settings, or run `gh auth login`."
            )
            return
        }

        token = value
        auth = GitHubAuth(
            hasToken: true,
            source: "gh auth token",
            ghPath: ghPath,
            detail: "Direct GitHub API access using the token from the authenticated gh CLI"
        )
    }

    func authentication() -> GitHubAuth {
        auth
    }

    func reloadAuthentication() async {
        prepared = false
        token = nil
        cache.removeAll()
        auth = GitHubAuth(hasToken: false, source: "none", ghPath: nil, detail: "Not checked yet")
        await prepare()
    }

    func invalidateCache() {
        cache.removeAll()
    }

    // MARK: - Repository and release reads

    func listOrganizationRepositories(organization: String) async throws -> [GitHubRepository] {
        var repositories: [GitHubRepository] = []
        for page in 1...5 {
            let url = makeURL(
                path: "/orgs/\(organization)/repos",
                query: [
                    URLQueryItem(name: "type", value: "all"),
                    URLQueryItem(name: "per_page", value: "100"),
                    URLQueryItem(name: "page", value: String(page)),
                ]
            )
            let data = try await requestData(url)
            let chunk = try decode([GitHubRepository].self, from: data)
            repositories.append(contentsOf: chunk)
            if chunk.count < 100 { break }
        }
        return repositories
    }

    func repository(_ fullName: String) async throws -> GitHubRepository? {
        let url = makeURL(path: "/repos/\(fullName)")
        do {
            let data = try await requestData(url, fresh: true)
            return try decode(GitHubRepository.self, from: data)
        } catch let GitHubClientError.http(status, _) where status == 404 {
            return nil
        }
    }

    func createRepository(organization: String, name: String, description: String) async throws -> GitHubRepository {
        let payload = CreateRepositoryPayload(
            name: name,
            description: description,
            privateRepo: false,
            autoInit: true
        )
        let body = try JSONEncoder().encode(payload)
        let url = makeURL(path: "/orgs/\(organization)/repos")
        let data = try await sendJSON(url: url, method: "POST", body: body)
        invalidateCache()
        return try decode(GitHubRepository.self, from: data)
    }

    func releases(repository: String) async throws -> [GitHubRelease] {
        var all: [GitHubRelease] = []
        for page in 1...10 {
            let url = makeURL(
                path: "/repos/\(repository)/releases",
                query: [
                    URLQueryItem(name: "per_page", value: "100"),
                    URLQueryItem(name: "page", value: String(page)),
                ]
            )
            let data = try await requestData(url)
            let chunk = try decode([GitHubRelease].self, from: data)
            all.append(contentsOf: chunk)
            if chunk.count < 100 { break }
        }
        return all
    }

    func release(repository: String, tag: String) async throws -> GitHubRelease? {
        let url = makeURL(path: "/repos/\(repository)/releases/tags/\(tag)")
        do {
            let data = try await requestData(url, fresh: true)
            return try decode(GitHubRelease.self, from: data)
        } catch let GitHubClientError.http(status, _) where status == 404 {
            return nil
        }
    }

    func inspect(_ repository: GitHubRepository, organization: String) async -> Mirror {
        var mirror = Mirror(
            name: repository.name,
            fullName: repository.fullName,
            htmlURL: repository.htmlURL
        )
        mirror.lastChecked = Date()

        do {
            let configText = try await file(
                repository: repository.fullName,
                path: "mirror.config.json",
                ref: nil
            )
            guard let configData = configText.data(using: .utf8) else {
                throw GitHubClientError.decoding("mirror.config.json is not UTF-8")
            }
            let config = try decode(MirrorConfig.self, from: configData)
            mirror.config = config
            mirror.sourceRepo = config.sourceRepo
            mirror.branch = config.effectiveBranch
            mirror.platforms = config.enabledPlatforms

            async let sourceRequest = releases(repository: config.sourceRepo)
            async let mirrorRequest = releases(repository: repository.fullName)
            let sourceReleases = try await sourceRequest
            let mirrorReleases = try await mirrorRequest

            let visibleSource = sourceReleases.filter { $0.draft != true }
            let likelyFirmware = visibleSource.filter { releaseHasFirmware($0, config: config) }
            let mirroredTags = Set(
                mirrorReleases
                    .filter { $0.draft != true }
                    .map(\.tagName)
            )
            let mirroredByTag = Dictionary(
                uniqueKeysWithValues: mirrorReleases
                    .filter { $0.draft != true }
                    .map { ($0.tagName, $0) }
            )

            let latest = likelyFirmware.first
            mirror.latestSourceTag = latest?.tagName
            mirror.latestSourceName = latest?.name
            mirror.latestSourcePublishedAt = parseGitHubDate(latest?.publishedAt)
            mirror.latestMirrorTag = mirrorReleases
                .filter { $0.draft != true }
                .first?
                .tagName
            mirror.newTags = likelyFirmware
                .map(\.tagName)
                .filter { !mirroredTags.contains($0) }

            for source in likelyFirmware {
                guard let existing = mirroredByTag[source.tagName] else { continue }
                var expected = expectedPackageNames(source: source, config: config)
                if let block = blockAssetNames(from: existing.body) {
                    expected = block
                }
                let actual = Set(existing.assets.map(\.name))
                let actualSizes = Dictionary(
                    existing.assets.map { ($0.name, $0.size ?? 0) },
                    uniquingKeysWith: { first, _ in first }
                )
                let maximum = config.maxAssetBytes ?? 2 * 1024 * 1024 * 1024
                let originals: [GitHubAsset] = config.copyOriginalAssets == false ? [] : source.assets.filter {
                    $0.name.lowercased() != "stats.txt" && ($0.size ?? 0) <= maximum
                }
                let originalsCurrent = originals.allSatisfy {
                    actualSizes[$0.name] == $0.size
                }
                if !expected.isEmpty && !expected.subtracting(actual).isEmpty {
                    mirror.incompleteTags.append(source.tagName)
                } else if existing.assets.isEmpty {
                    mirror.incompleteTags.append(source.tagName)
                } else if !originalsCurrent {
                    mirror.incompleteTags.append(source.tagName)
                }
            }

            _ = organization
        } catch {
            mirror.fetchError = error.localizedDescription
        }
        return mirror
    }

    func fileDocument(repository: String, path: String, ref: String?, fresh: Bool = false) async throws -> GitHubFileDocument {
        let url = makeURL(
            path: "/repos/\(repository)/contents/\(path)",
            query: ref.map { [URLQueryItem(name: "ref", value: $0)] } ?? []
        )
        let data = try await requestData(url, fresh: fresh)
        let payload = try decode(ContentsPayload.self, from: data)
        guard let content = payload.content else {
            throw GitHubClientError.decoding("\(path) has no file content")
        }
        guard let decoded = Data(base64Encoded: content, options: .ignoreUnknownCharacters) else {
            throw GitHubClientError.decoding("\(path) is not valid base64")
        }
        guard let text = String(data: decoded, encoding: .utf8) else {
            throw GitHubClientError.decoding("\(path) is not UTF-8")
        }
        return GitHubFileDocument(text: text, sha: payload.sha)
    }

    func file(repository: String, path: String, ref: String?, fresh: Bool = false) async throws -> String {
        try await fileDocument(repository: repository, path: path, ref: ref, fresh: fresh).text
    }

    // MARK: - Release writes

    func createRelease(
        repository: String,
        tag: String,
        title: String,
        body: String,
        prerelease: Bool
    ) async throws -> GitHubRelease {
        let payload = CreateReleasePayload(
            tagName: tag,
            name: title,
            body: body,
            draft: false,
            prerelease: prerelease
        )
        let data = try JSONEncoder().encode(payload)
        let url = makeURL(path: "/repos/\(repository)/releases")
        let response = try await sendJSON(url: url, method: "POST", body: data)
        invalidateCache()
        return try decode(GitHubRelease.self, from: response)
    }

    func updateRelease(
        repository: String,
        releaseID: Int,
        title: String?,
        body: String?,
        prerelease: Bool?
    ) async throws -> GitHubRelease {
        let payload = UpdateReleasePayload(
            name: title,
            body: body,
            prerelease: prerelease
        )
        let data = try JSONEncoder().encode(payload)
        let url = makeURL(path: "/repos/\(repository)/releases/\(releaseID)")
        let response = try await sendJSON(url: url, method: "PATCH", body: data)
        invalidateCache()
        return try decode(GitHubRelease.self, from: response)
    }

    func deleteAsset(repository: String, assetID: Int) async throws {
        let url = makeURL(path: "/repos/\(repository)/releases/assets/\(assetID)")
        var request = authorizedRequest(url: url)
        request.httpMethod = "DELETE"
        let (data, response) = try await session.data(for: request)
        try validate(data: data, response: response)
        invalidateCache()
    }

    func uploadAsset(
        repository: String,
        releaseID: Int,
        fileURL: URL,
        name: String
    ) async throws -> GitHubAsset {
        var components = URLComponents()
        components.scheme = "https"
        components.host = "uploads.github.com"
        components.path = "/repos/\(repository)/releases/\(releaseID)/assets"
        components.queryItems = [URLQueryItem(name: "name", value: name)]
        guard let url = components.url else { throw GitHubClientError.badResponse }
        var request = authorizedRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.upload(for: request, fromFile: fileURL)
        try validate(data: data, response: response)
        invalidateCache()
        return try decode(GitHubAsset.self, from: data)
    }

    func putFile(
        repository: String,
        path: String,
        text: String,
        message: String,
        branch: String,
        sha: String?
    ) async throws {
        let payload = PutFilePayload(
            message: message,
            content: Data(text.utf8).base64EncodedString(),
            branch: branch,
            sha: sha
        )
        let body = try JSONEncoder().encode(payload)
        let url = makeURL(path: "/repos/\(repository)/contents/\(path)")
        _ = try await sendJSON(url: url, method: "PUT", body: body)
        invalidateCache()
    }

    // MARK: - Binary downloads

    func downloadAsset(repository: String, asset: GitHubAsset, to destination: URL) async throws {
        guard let id = asset.id else {
            throw GitHubClientError.decoding("Asset \(asset.name) has no id")
        }
        let url = makeURL(path: "/repos/\(repository)/releases/assets/\(id)")
        var request = authorizedRequest(url: url)
        request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
        let (temporary, response) = try await session.download(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw GitHubClientError.badResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let data = (try? Data(contentsOf: temporary)) ?? Data()
            try validate(data: data, response: response)
            throw GitHubClientError.http(http.statusCode, "Download failed")
        }
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        try FileManager.default.moveItem(at: temporary, to: destination)
    }

    // MARK: - HTTP plumbing

    private func makeURL(path: String, query: [URLQueryItem] = []) -> URL {
        var components = URLComponents()
        components.scheme = "https"
        components.host = "api.github.com"
        components.path = path
        components.queryItems = query.isEmpty ? nil : query
        return components.url!
    }

    private func authorizedRequest(url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        request.setValue("MirrorWatch/0.2", forHTTPHeaderField: "User-Agent")
        if let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func requestData(_ url: URL, fresh: Bool = false) async throws -> Data {
        let key = url.absoluteString
        var request = authorizedRequest(url: url)
        request.httpMethod = "GET"
        if !fresh, let cached = cache[key] {
            request.setValue(cached.etag, forHTTPHeaderField: "If-None-Match")
        }
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse,
           http.statusCode == 304,
           let cached = cache[key] {
            return cached.data
        }
        try validate(data: data, response: response)
        if let http = response as? HTTPURLResponse,
           let etag = http.value(forHTTPHeaderField: "ETag"),
           !etag.isEmpty {
            cache[key] = CacheEntry(etag: etag, data: data)
        }
        return data
    }

    private func sendJSON(url: URL, method: String, body: Data) async throws -> Data {
        var request = authorizedRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        let (data, response) = try await session.data(for: request)
        try validate(data: data, response: response)
        return data
    }

    private func validate(data: Data, response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else {
            throw GitHubClientError.badResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let message = (payload?["message"] as? String)
                ?? String(data: data, encoding: .utf8)
                ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            if http.statusCode == 403,
               http.value(forHTTPHeaderField: "x-ratelimit-remaining") == "0" {
                let reset = http.value(forHTTPHeaderField: "x-ratelimit-reset")
                    .flatMap(Double.init)
                    .map { Date(timeIntervalSince1970: $0) }
                let when = reset.map {
                    " Try again at \($0.formatted(date: .omitted, time: .shortened))."
                } ?? ""
                throw GitHubClientError.http(403, "API rate limit reached.\(when)")
            }
            if http.statusCode == 401 {
                throw GitHubClientError.http(401, "The saved GitHub token is invalid or expired.")
            }
            throw GitHubClientError.http(http.statusCode, message)
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw GitHubClientError.decoding(error.localizedDescription)
        }
    }

    private struct ContentsPayload: Decodable {
        let content: String?
        let sha: String?
    }

    private struct CreateRepositoryPayload: Encodable {
        let name: String
        let description: String
        let privateRepo: Bool
        let autoInit: Bool

        enum CodingKeys: String, CodingKey {
            case name
            case description
            case privateRepo = "private"
            case autoInit = "auto_init"
        }
    }

    private struct CreateReleasePayload: Encodable {
        let tagName: String
        let name: String
        let body: String
        let draft: Bool
        let prerelease: Bool

        enum CodingKeys: String, CodingKey {
            case tagName = "tag_name"
            case name
            case body
            case draft
            case prerelease
        }
    }

    private struct UpdateReleasePayload: Encodable {
        let name: String?
        let body: String?
        let prerelease: Bool?
    }

    private struct PutFilePayload: Encodable {
        let message: String
        let content: String
        let branch: String
        let sha: String?
    }
}

private let defaultFirmwareIncludes = ["*.zip"]
private let defaultFirmwareExcludes = [
    "*-windows.zip",
    "*-windows-arm64.zip",
    "*-linux.zip",
    "*-linux-arm64.zip",
    "*-macos.zip",
    "*portable*.zip",
    "*source*.zip",
    "*installer*.zip",
    "*.dmg",
    "*.apk",
    "*.tar.gz",
    "*.tgz",
    "*.tar",
    "*.sha256",
    "*.json",
    "*.txt",
    "*.sig",
    "*.img.gz",
    "*.exe",
    "*.appimage",
]
private let outputSuffixes = [
    "-windows.zip",
    "-windows-arm64.zip",
    "-linux.zip",
    "-linux-arm64.zip",
    "-macos.zip",
]

func releaseHasFirmware(_ release: GitHubRelease, config: MirrorConfig) -> Bool {
    !firmwareAssets(in: release, config: config).isEmpty
}

func firmwareAssets(in release: GitHubRelease, config: MirrorConfig) -> [GitHubAsset] {
    let includes = config.firmware?.include.flatMap { $0.isEmpty ? nil : $0 } ?? defaultFirmwareIncludes
    let excludes = config.firmware?.exclude.flatMap { $0.isEmpty ? nil : $0 } ?? defaultFirmwareExcludes
    let maximum = config.maxAssetBytes ?? 2 * 1024 * 1024 * 1024
    return release.assets.filter { asset in
        let name = asset.name.lowercased()
        guard name.hasSuffix(".zip") else { return false }
        guard Int(asset.size ?? 0) <= maximum else { return false }
        guard !outputSuffixes.contains(where: { name.hasSuffix($0) }) else { return false }
        guard !excludes.contains(where: { wildcardMatch(name, pattern: $0) }) else { return false }
        return includes.contains(where: { wildcardMatch(name, pattern: $0) })
    }
}

func expectedPackageNames(source: GitHubRelease, config: MirrorConfig) -> Set<String> {
    let platforms = config.enabledPlatforms.filter { !$0.optional }
    var names = Set<String>()
    for asset in firmwareAssets(in: source, config: config) {
        let base = asset.name.lowercased().hasSuffix(".zip")
            ? String(asset.name.dropLast(4))
            : asset.name
        for platform in platforms {
            names.insert(base + platform.suffix)
        }
    }
    return names
}

func blockAssetNames(from body: String?) -> Set<String>? {
    guard let body else { return nil }
    for marker in ["portable-mirror", "native-mirror"] {
        guard let start = body.range(of: "<!-- \(marker):"),
              let end = body.range(of: "-->", range: start.upperBound..<body.endIndex) else {
            continue
        }
        let json = String(body[start.upperBound..<end.lowerBound])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = json.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let assets = object["assets"] as? [[String: Any]] else {
            continue
        }
        return Set(assets.compactMap { $0["name"] as? String })
    }
    return nil
}
