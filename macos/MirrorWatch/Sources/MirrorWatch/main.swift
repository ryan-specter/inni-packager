import AppKit
import Foundation

let arguments = CommandLine.arguments
setbuf(stdout, nil)
setbuf(stderr, nil)

func argumentValue(_ flag: String) -> String? {
    guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else {
        return nil
    }
    return arguments[index + 1]
}

func runAsync(_ operation: @escaping @MainActor () async -> Int32) -> Int32 {
    let done = DispatchSemaphore(value: 0)
    var code: Int32 = 1
    Task { @MainActor in
        code = await operation()
        done.signal()
    }
    while done.wait(timeout: .now() + 0.05) == .timedOut {
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
    }
    return code
}

if arguments.contains("--selftest") {
    let code = runAsync {
        do {
            try await NativeSelfTest.run()
            return 0
        } catch {
            fputs("Native packager self-test failed: \(error.localizedDescription)\n", stderr)
            return 1
        }
    }
    exit(code)
}

if arguments.contains("--check") {
    let code = runAsync {
        let service = MirrorService()
        return await service.checkForCLI()
    }
    exit(code)
}

if arguments.contains("--cache-apps") {
    let code = runAsync {
        let service = MirrorService()
        return await service.warmPortableAppsForCLI()
    }
    exit(code)
}

if arguments.contains("--replace-macos") {
    let mirror = argumentValue("--mirror")
    let tag = argumentValue("--tag")
    let code = runAsync {
        let service = MirrorService()
        return await service.syncAllForCLI(
            forceMacos: true,
            onlyMirror: mirror,
            tags: tag.map { [$0] }
        )
    }
    exit(code)
}

if arguments.contains("--sync-all") {
    let code = runAsync {
        let service = MirrorService()
        return await service.syncAllForCLI()
    }
    exit(code)
}

MirrorWatchApp.main()
