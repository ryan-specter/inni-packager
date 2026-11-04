import AppKit
import Combine
import Foundation

@MainActor
final class MirrorService: ObservableObject {
    static let shared = MirrorService()

    @Published private(set) var mirrors: [Mirror] = []
    @Published private(set) var portableApps: [String: PortableAppAsset] = [:]
    @Published private(set) var isRefreshing = false
    @Published private(set) var isRefreshingApps = false
    @Published private(set) var lastRefresh: Date?
    @Published private(set) var activity: [ActivityLine] = []
    @Published private(set) var auth = GitHubAuth(
        hasToken: false,
        source: "none",
        ghPath: nil,
        detail: "Checking GitHub..."
    )
    @Published private(set) var currentSync: SyncProgress?
    @Published private(set) var queueCount = 0
    @Published private(set) var globalError: String?
    @Published private(set) var manifestProductCount = 0
    @Published var autoSync: Bool {
        didSet {
            defaults.set(autoSync, forKey: Keys.autoSync)
            if autoSync { queueAutomaticSyncIfNeeded() }
        }
    }
    @Published var pollInterval: Int {
        didSet {
            defaults.set(pollInterval, forKey: Keys.pollInterval)
            schedulePolling()
        }
    }
    @Published var organization: String {
        didSet { defaults.set(organization, forKey: Keys.organization) }
    }
    @Published var manifestURL: String {
        didSet { defaults.set(manifestURL, forKey: Keys.manifestURL) }
    }

    private enum Keys {
        static let autoSync = "nativeAutoSync"
        static let pollInterval = "nativePollInterval"
        static let organization = "organization"
        static let manifestURL = "manifestURL"
        static let autoAttempted = "nativeAutoAttempted"
    }

    private let defaults = UserDefaults.standard
    private let client = GitHubClient()
    private var appStore: PortableAppStore?
    private var pollTask: Task<Void, Never>?
    private var queueTask: Task<Void, Never>?
    private var appTask: Task<Void, Never>?
    private var wakeCancellable: AnyCancellable?
    private var started = false
    private var shuttingDown = false
    private var queue: [SyncJob] = []
    private var activeEngine: NativeSyncEngine?
    private var autoAttempted: [String: Date] = [:]
    private var lastManifestCheck: Date?

    init() {
        autoSync = (defaults.object(forKey: Keys.autoSync) as? Bool) ?? true
        let storedInterval = defaults.object(forKey: Keys.pollInterval) as? Int
        pollInterval = max(30, storedInterval ?? 60)
        organization = defaults.string(forKey: Keys.organization) ?? "y1-community"
        manifestURL = defaults.string(forKey: Keys.manifestURL) ?? ManifestService.defaultURL
        appStore = try? PortableAppStore(client: client)

        if let raw = defaults.dictionary(forKey: Keys.autoAttempted) {
            for (key, value) in raw {
                if let timestamp = value as? Double {
                    autoAttempted[key] = Date(timeIntervalSince1970: timestamp)
                }
            }
        }

        wakeCancellable = NSWorkspace.shared.notificationCenter
            .publisher(for: NSWorkspace.didWakeNotification)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                Task { @MainActor in await self?.refresh() }
            }
    }

    var newReleaseCount: Int {
        mirrors.reduce(0) { $0 + $1.newTags.count }
    }

    var mirrorsNeedingAttention: Int {
        mirrors.filter(\.needsAttention).count
    }

    var canSync: Bool {
        auth.hasToken && appStore != nil
    }

    var setupMessage: String? {
        if !auth.hasToken { return auth.detail }
        if appStore == nil { return "Could not create Mirror Watch's Application Support folder." }
        return nil
    }

    var menuBarSymbol: String {
        if isRefreshing || currentSync != nil { return "arrow.triangle.2.circlepath" }
        if mirrors.contains(where: { $0.fetchError != nil }) { return "exclamationmark.triangle.fill" }
        if newReleaseCount > 0 { return "arrow.down.circle.fill" }
        return "arrow.triangle.2.circlepath.circle"
    }

    var menuBarSummary: String {
        if let currentSync {
            return "Packing \(currentSync.mirrorName): \(currentSync.label)"
        }
        if isRefreshing { return "Checking manifest and mirrors..." }
        if isRefreshingApps { return "Updating portable app cache..." }
        if newReleaseCount > 0 {
            return "\(newReleaseCount) release\(newReleaseCount == 1 ? "" : "s") waiting"
        }
        if !mirrors.isEmpty { return "\(mirrors.count) mirrors watched" }
        return "Mirror Watch"
    }

    var lastActivity: ActivityLine? {
        activity.last
    }

    func start() {
        guard !started else { return }
        started = true
        log("Mirror Watch started. Manifest: \(manifestURL)", kind: .info)
        schedulePolling()
        Task { await refresh() }
    }

    func refreshNow() {
        lastManifestCheck = nil
        Task { await refresh() }
    }

    func refresh(
        allowAuto: Bool = true,
        ensureManifest: Bool = true,
        downloadApps: Bool = true
    ) async {
        guard !shuttingDown, !isRefreshing else { return }
        isRefreshing = true
        globalError = nil
        defer { isRefreshing = false }

        await client.prepare()
        auth = await client.authentication()
        if !auth.hasToken {
            await client.reloadAuthentication()
            auth = await client.authentication()
        }
        guard auth.hasToken else {
            globalError = auth.detail
            log(auth.detail, kind: .error)
            return
        }

        if ensureManifest,
           lastManifestCheck == nil || Date().timeIntervalSince(lastManifestCheck!) > 300 {
            do {
                let data = try await ManifestService.fetch(url: manifestURL)
                let products = try await ManifestService.ensureMirrors(
                    client: client,
                    organization: organization,
                    manifestData: data,
                    progress: { [weak self] line in
                        Task { @MainActor in self?.log(line, kind: .info) }
                    }
                )
                manifestProductCount = products.count
                lastManifestCheck = Date()
                log("Manifest: \(products.count) source product\(products.count == 1 ? "" : "s") resolved.", kind: .info)
            } catch {
                log("Manifest check failed: \(error.localizedDescription)", kind: .warning)
            }
        }

        do {
            let repositories = try await client.listOrganizationRepositories(organization: organization)
            let mirrorRepos = repositories.filter {
                $0.name.hasSuffix("-mirror") && $0.archived != true
            }
            guard !mirrorRepos.isEmpty else {
                globalError = "No *-mirror repositories found in \(organization)."
                return
            }
            log("Checking \(mirrorRepos.count) mirror repositories...", kind: .info)

            let oldByName = Dictionary(uniqueKeysWithValues: mirrors.map { ($0.name, $0) })
            let client = self.client
            let org = organization
            let checked = await withTaskGroup(of: Mirror.self, returning: [Mirror].self) { group in
                for repository in mirrorRepos {
                    group.addTask {
                        await client.inspect(repository, organization: org)
                    }
                }
                var result: [Mirror] = []
                for await mirror in group { result.append(mirror) }
                return result
            }

            mirrors = checked.map { fresh in
                var merged = fresh
                if let old = oldByName[fresh.name] {
                    merged.lastSync = old.lastSync
                    if merged.fetchError == nil { merged.syncError = old.syncError }
                }
                return merged
            }
            .sorted { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending }

            lastRefresh = Date()
            let failed = mirrors.filter { $0.fetchError != nil }.count
            if failed > 0 {
                log("Checked \(mirrors.count) mirrors; \(failed) could not be read.", kind: .warning)
            } else {
                log(
                    "Checked \(mirrors.count) mirrors. \(newReleaseCount) release\(newReleaseCount == 1 ? "" : "s") waiting.",
                    kind: newReleaseCount > 0 ? .warning : .success
                )
            }
            if downloadApps {
                startPortableAppRefresh()
            }
            if allowAuto, autoSync {
                queueAutomaticSyncIfNeeded()
            }
        } catch {
            globalError = error.localizedDescription
            log("Refresh failed: \(error.localizedDescription)", kind: .error)
        }
    }

    func startPortableAppRefresh() {
        guard appTask == nil, !isRefreshingApps, let appStore else { return }
        isRefreshingApps = true
        appTask = Task { [weak self] in
            guard let self else { return }
            do {
                let updated = try await appStore.refresh { [weak self] line in
                    Task { @MainActor in self?.log(line, kind: .info) }
                }
                await MainActor.run {
                    self.portableApps = updated
                    self.isRefreshingApps = false
                    self.appTask = nil
                }
            } catch {
                await MainActor.run {
                    self.log("Portable app refresh failed: \(error.localizedDescription)", kind: .warning)
                    self.isRefreshingApps = false
                    self.appTask = nil
                }
            }
        }
    }

    func sync(_ mirror: Mirror) {
        enqueue(SyncJob(mirrorName: mirror.name, tags: nil, automatic: false))
    }

    func syncAllNew() {
        for mirror in mirrors where !mirror.newTags.isEmpty || !mirror.incompleteTags.isEmpty {
            enqueue(SyncJob(mirrorName: mirror.name, tags: nil, automatic: false))
        }
    }

    func stopSync() {
        queue.removeAll()
        queueCount = 0
        if var progress = currentSync {
            progress.stopping = true
            progress.line = "Stopping after the current file..."
            currentSync = progress
        }
        let engine = activeEngine
        Task { await engine?.cancel() }
        log("Stopping the current job...", kind: .warning)
    }

    func shutdown() {
        shuttingDown = true
        pollTask?.cancel()
        appTask?.cancel()
        queue.removeAll()
        queueCount = 0
        let engine = activeEngine
        Task { await engine?.cancel() }
    }

    func applySettings(
        organization: String,
        pollInterval: Int,
        autoSync: Bool,
        manifestURL: String,
        token: String?
    ) {
        let cleanOrg = organization.trimmingCharacters(in: .whitespacesAndNewlines)
        self.organization = cleanOrg.isEmpty ? "y1-community" : cleanOrg
        self.pollInterval = max(30, pollInterval)
        self.autoSync = autoSync
        let cleanManifest = manifestURL.trimmingCharacters(in: .whitespacesAndNewlines)
        self.manifestURL = cleanManifest.isEmpty ? ManifestService.defaultURL : cleanManifest
        var tokenChanged = false
        if let token = token?.trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty {
            do {
                try TokenStore.save(token)
                tokenChanged = true
                log("Saved GitHub token to Keychain.", kind: .success)
            } catch {
                log(error.localizedDescription, kind: .error)
            }
        }
        lastManifestCheck = nil
        Task {
            if tokenChanged {
                await client.reloadAuthentication()
                auth = await client.authentication()
            }
            await refresh()
        }
    }

    func clearToken() {
        TokenStore.delete()
        Task {
            await client.reloadAuthentication()
            auth = await client.authentication()
            log("Removed the saved GitHub token.", kind: .warning)
            await refresh()
        }
    }

    func open(_ url: URL?) {
        guard let url else { return }
        NSWorkspace.shared.open(url)
    }

    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        log("Copied to clipboard.", kind: .info)
    }

    func checkForCLI() async -> Int32 {
        await client.prepare()
        auth = await client.authentication()
        print("Mirror Watch \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev")")
        print("GitHub: \(auth.detail)")
        guard auth.hasToken else {
            print("ERROR: \(auth.detail)")
            return 1
        }
        await refresh(allowAuto: false, ensureManifest: false, downloadApps: false)
        if let globalError {
            print("ERROR: \(globalError)")
            return 1
        }
        print("Organization: \(organization)")
        print("Checked: \(lastRefresh.map { $0.formatted(date: .abbreviated, time: .standard) } ?? "never")")
        print("")
        for mirror in mirrors {
            let status: String
            if let error = mirror.fetchError {
                status = "ERROR \(error)"
            } else if !mirror.newTags.isEmpty {
                status = "NEW \(mirror.newTags.joined(separator: ","))"
            } else if !mirror.incompleteTags.isEmpty {
                status = "INCOMPLETE \(mirror.incompleteTags.joined(separator: ","))"
            } else {
                status = "CURRENT"
            }
            print(String(format: "%-34@ %@", mirror.name as NSString, status))
        }
        do {
            let data = try await ManifestService.fetch(url: manifestURL)
            let products = try ManifestService.products(from: data, organization: organization)
            let known = Set(mirrors.map { $0.fullName.lowercased() })
            let missing = products.filter { !known.contains($0.mirrorRepo.lowercased()) }
            print("Manifest products: \(products.count)")
            if missing.isEmpty {
                print("Manifest mirrors: all present")
            } else {
                print("Missing mirrors: \(missing.map(\.mirrorRepo).joined(separator: ", "))")
            }
        } catch {
            print("Manifest: ERROR \(error.localizedDescription)")
            return 1
        }
        let failures = mirrors.filter { $0.fetchError != nil }.count
        if failures > 0 {
            print("\n\(failures) mirror(s) could not be checked.")
            return 1
        }
        print("\n\(mirrors.count) mirrors checked; \(newReleaseCount) new release(s).")
        return 0
    }

    func warmPortableAppsForCLI() async -> Int32 {
        await client.prepare()
        auth = await client.authentication()
        guard auth.hasToken else {
            print("ERROR: \(auth.detail)")
            return 1
        }
        guard let appStore else {
            print("ERROR: could not create the portable app cache")
            return 1
        }
        do {
            let updated = try await appStore.refresh { line in
                print(line)
            }
            for spec in PortableAppStore.specs {
                if let asset = updated[spec.key] {
                    print("\(spec.label): \(asset.tag) \(asset.sizeText)")
                } else {
                    print("\(spec.label): not published")
                }
            }
            return updated.count >= 3 ? 0 : 1
        } catch {
            print("ERROR: \(error.localizedDescription)")
            return 1
        }
    }

    func syncAllForCLI(
        forceMacos: Bool = false,
        onlyMirror: String? = nil,
        tags: [String]? = nil
    ) async -> Int32 {
        await client.prepare()
        auth = await client.authentication()
        print("Mirror Watch \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev")")
        print("GitHub: \(auth.detail)")
        guard auth.hasToken else {
            print("ERROR: \(auth.detail)")
            return 1
        }
        guard let appStore else {
            print("ERROR: could not create the portable app cache")
            return 1
        }

        print("Resolving manifest and mirror configuration...")
        await refresh(allowAuto: false, ensureManifest: true, downloadApps: false)
        if let globalError {
            print("ERROR: \(globalError)")
            return 1
        }
        guard !mirrors.isEmpty else {
            print("ERROR: no mirrors found in \(organization)")
            return 1
        }
        if forceMacos {
            print("Mode: replacing macOS packages with the Innioasis DMG + firmware layout.")
        }

        do {
            _ = try await appStore.refresh { line in print(line) }
        } catch {
            print("ERROR: portable app cache: \(error.localizedDescription)")
            return 1
        }

        var selectedMirrors = mirrors.sorted(by: { $0.name < $1.name })
        if let onlyMirror {
            selectedMirrors = selectedMirrors.filter {
                $0.name.caseInsensitiveCompare(onlyMirror) == .orderedSame
                    || $0.fullName.caseInsensitiveCompare(onlyMirror) == .orderedSame
            }
            guard !selectedMirrors.isEmpty else {
                print("ERROR: mirror not found: \(onlyMirror)")
                return 1
            }
        }

        var runFailures: [String] = []
        for mirror in selectedMirrors {
            print("\n=== \(mirror.fullName) ===")
            if let fetchError = mirror.fetchError {
                print("SKIP: \(fetchError)")
                runFailures.append("\(mirror.name): \(fetchError)")
                continue
            }
            guard mirror.config != nil else {
                print("SKIP: no mirror.config.json")
                runFailures.append("\(mirror.name): no mirror.config.json")
                continue
            }

            let engine = NativeSyncEngine(client: client, apps: appStore) { line in
                print(line)
            }
            do {
                let report = try await engine.sync(
                    mirror: mirror,
                    tags: tags,
                    forceMacos: forceMacos
                )
                print(
                    "RESULT: \(report.builtAssets.count) package(s) uploaded, "
                    + "\(report.currentTags.count) release(s) already current, "
                    + "\(report.failures.count) failure(s)."
                )
                for failure in report.failures {
                    print("FAILED: \(failure)")
                    runFailures.append("\(mirror.name): \(failure)")
                }
            } catch is CancellationError {
                print("ERROR: cancelled")
                runFailures.append("\(mirror.name): cancelled")
            } catch {
                print("ERROR: \(error.localizedDescription)")
                runFailures.append("\(mirror.name): \(error.localizedDescription)")
            }
            await client.invalidateCache()
        }

        print("\nRechecking every mirror...")
        await refresh(allowAuto: false, ensureManifest: false, downloadApps: false)
        print("")
        for mirror in mirrors.sorted(by: { $0.name < $1.name }) {
            let state: String
            if let error = mirror.fetchError {
                state = "ERROR \(error)"
            } else if !mirror.newTags.isEmpty {
                state = "NEW \(mirror.newTags.joined(separator: ","))"
            } else if !mirror.incompleteTags.isEmpty {
                state = "INCOMPLETE \(mirror.incompleteTags.joined(separator: ","))"
            } else {
                state = "CURRENT"
            }
            print(String(format: "%-34@ %@", mirror.name as NSString, state))
        }

        let finalNew = mirrors.reduce(0) { $0 + $1.newTags.count }
        let finalIncomplete = mirrors.reduce(0) { $0 + $1.incompleteTags.count }
        print("\n\(mirrors.count) mirrors checked; \(finalNew) new tag(s), \(finalIncomplete) incomplete release(s).")
        if !runFailures.isEmpty {
            print("\(runFailures.count) sync failure(s):")
            for failure in runFailures {
                print("  - \(failure)")
            }
        }
        return runFailures.isEmpty && finalNew == 0 && finalIncomplete == 0 ? 0 : 1
    }

    // MARK: - Queue

    private func queueAutomaticSyncIfNeeded() {
        guard autoSync, !shuttingDown, canSync else { return }
        let now = Date()
        for mirror in mirrors where !mirror.newTags.isEmpty || !mirror.incompleteTags.isEmpty {
            if let attempted = autoAttempted[mirror.name],
               now.timeIntervalSince(attempted) < 6 * 3600 {
                continue
            }
            autoAttempted[mirror.name] = now
            persistAutoAttempted()
            log("Auto-queued \(mirror.name) for packing.", kind: .info)
            enqueue(SyncJob(mirrorName: mirror.name, tags: nil, automatic: true))
        }
    }

    private func enqueue(_ job: SyncJob) {
        guard !shuttingDown, canSync else { return }
        if queue.contains(where: { $0.mirrorName == job.mirrorName }) ||
            currentSync?.mirrorName == job.mirrorName {
            return
        }
        queue.append(job)
        queueCount = queue.count
        startQueueIfNeeded()
    }

    private func startQueueIfNeeded() {
        guard queueTask == nil, !queue.isEmpty else { return }
        queueTask = Task { [weak self] in
            await self?.drainQueue()
        }
    }

    private func drainQueue() async {
        while !shuttingDown && !queue.isEmpty {
            let job = queue.removeFirst()
            queueCount = queue.count
            guard let mirror = mirrors.first(where: { $0.name == job.mirrorName }),
                  let appStore else { continue }
            currentSync = SyncProgress(
                mirrorName: mirror.name,
                label: job.label,
                startedAt: Date(),
                line: "Preparing native packager..."
            )
            log("Syncing \(mirror.name) (\(job.label))", kind: .info)
            let engine = NativeSyncEngine(client: client, apps: appStore) { [weak self] line in
                Task { @MainActor in self?.handleSyncLine(line) }
            }
            activeEngine = engine
            do {
                let report = try await engine.sync(mirror: mirror, tags: job.tags)
                if report.failures.isEmpty {
                    log("Finished \(mirror.name): \(report.builtAssets.count) package(s) uploaded.", kind: .success)
                    updateSyncResult(name: mirror.name, success: true, error: nil)
                } else {
                    let message = report.failures.joined(separator: "; ")
                    log("Finished \(mirror.name) with errors: \(message)", kind: .error)
                    updateSyncResult(name: mirror.name, success: false, error: message)
                }
            } catch is CancellationError {
                log("Stopped \(mirror.name).", kind: .warning)
                updateSyncResult(name: mirror.name, success: false, error: "Stopped")
            } catch {
                log("Sync failed for \(mirror.name): \(error.localizedDescription)", kind: .error)
                updateSyncResult(name: mirror.name, success: false, error: error.localizedDescription)
            }
            activeEngine = nil
            currentSync = nil
            await client.invalidateCache()
            await refresh(allowAuto: false, ensureManifest: false)
        }
        queueTask = nil
        if autoSync, !shuttingDown {
            queueAutomaticSyncIfNeeded()
        }
    }

    private func updateSyncResult(name: String, success: Bool, error: String?) {
        guard let index = mirrors.firstIndex(where: { $0.name == name }) else { return }
        if success {
            mirrors[index].lastSync = Date()
            mirrors[index].syncError = nil
        } else {
            mirrors[index].syncError = error
        }
    }

    private func handleSyncLine(_ line: String) {
        if var progress = currentSync {
            progress.line = line
            currentSync = progress
        }
        appendActivity(line, kind: lineKind(line))
    }

    private func lineKind(_ line: String) -> ActivityKind {
        let lower = line.lowercased()
        if lower.contains("failed") || lower.contains("error") || lower.contains("traceback") {
            return .error
        }
        if lower.contains("warning") || lower.contains("skipping") || lower.contains("missing") {
            return .warning
        }
        if lower.contains("uploaded") || lower.contains("created") || lower.contains("finished")
            || lower.contains("updated") || lower.contains("stored") {
            return .success
        }
        return .info
    }

    private func log(_ text: String, kind: ActivityKind) {
        appendActivity(text, kind: kind)
    }

    private func appendActivity(_ text: String, kind: ActivityKind) {
        activity.append(ActivityLine(date: Date(), text: text, kind: kind))
        if activity.count > 800 {
            activity.removeFirst(activity.count - 800)
        }
    }

    private func persistAutoAttempted() {
        defaults.set(autoAttempted.mapValues(\.timeIntervalSince1970), forKey: Keys.autoAttempted)
    }

    private func schedulePolling() {
        pollTask?.cancel()
        guard !shuttingDown else { return }
        let seconds = max(30, pollInterval)
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: UInt64(seconds) * 1_000_000_000)
                } catch {
                    return
                }
                if Task.isCancelled { return }
                await self?.refresh()
            }
        }
    }
}
