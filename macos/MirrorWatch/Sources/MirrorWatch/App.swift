import AppKit
import SwiftUI

final class MirrorWatchAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            MirrorService.shared.start()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            MirrorService.shared.shutdown()
        }
    }
}

struct MirrorWatchApp: App {
    @NSApplicationDelegateAdaptor(MirrorWatchAppDelegate.self) private var appDelegate
    @StateObject private var service = MirrorService.shared

    var body: some Scene {
        MenuBarExtra {
            DashboardView(service: service)
        } label: {
            MenuBarLabel(service: service)
        }
        .menuBarExtraStyle(.window)
    }
}

struct MenuBarLabel: View {
    @ObservedObject var service: MirrorService

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: service.menuBarSymbol)
            if service.newReleaseCount > 0 {
                Text("\(min(service.newReleaseCount, 99))")
                    .font(.system(size: 10, weight: .bold, design: .rounded))
                    .monospacedDigit()
            }
        }
        .help(service.menuBarSummary)
        .accessibilityLabel("Mirror Watch. \(service.menuBarSummary)")
    }
}
