import AppKit
import SwiftUI

struct DashboardView: View {
    @ObservedObject var service: MirrorService
    @State private var showSettings = false

    var body: some View {
        Group {
            if showSettings {
                SettingsView(service: service, isPresented: $showSettings)
            } else {
                dashboard
            }
        }
        .frame(width: 440, height: 620)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear { service.start() }
    }

    private var dashboard: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let message = service.setupMessage {
                setupBanner(message)
            }
            statusCard
            stats
            activityHeader
            activityFeed
            Divider()
            footer
        }
    }

    private var header: some View {
        HStack(spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(
                        LinearGradient(
                            colors: [Color.teal, Color.blue],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                Image(systemName: "shippingbox.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(.white)
            }
            .frame(width: 34, height: 34)

            VStack(alignment: .leading, spacing: 2) {
                Text("Mirror Watch")
                    .font(.system(size: 15, weight: .semibold))
                HStack(spacing: 5) {
                    Circle()
                        .fill(statusColor)
                        .frame(width: 6, height: 6)
                    Text(service.menuBarSummary)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 8)

            Button {
                service.refreshNow()
            } label: {
                Image(systemName: "arrow.clockwise")
                    .frame(width: 24, height: 24)
            }
            .buttonStyle(.borderless)
            .disabled(service.isRefreshing)
            .help("Check the manifest and GitHub now")

            Button {
                showSettings = true
            } label: {
                Image(systemName: "gearshape")
                    .frame(width: 24, height: 24)
            }
            .buttonStyle(.borderless)
            .help("Settings")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }

    private var statusColor: Color {
        if service.globalError != nil { return .red }
        if service.currentSync != nil || service.isRefreshing { return .blue }
        if service.newReleaseCount > 0 { return .orange }
        if !service.mirrors.isEmpty { return .green }
        return .secondary
    }

    private func setupBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .font(.system(size: 12))
                .padding(.top, 1)
            Text(message)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button("Fix") { showSettings = true }
                .font(.system(size: 11, weight: .semibold))
                .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Color.orange.opacity(0.1))
    }

    private var statusCard: some View {
        HStack(spacing: 10) {
            if let current = service.currentSync {
                ProgressView()
                    .controlSize(.small)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Packing \(current.mirrorName)")
                        .font(.system(size: 12, weight: .semibold))
                    Text(current.line)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Spacer()
                Button {
                    service.stopSync()
                } label: {
                    Image(systemName: "stop.circle.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(.red)
                }
                .buttonStyle(.borderless)
                .help("Stop after the current step")
            } else if service.isRefreshing {
                ProgressView()
                    .controlSize(.small)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Checking manifest and releases")
                        .font(.system(size: 12, weight: .semibold))
                    Text("Reading source repositories and matching mirror coverage")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                Spacer()
            } else if service.isRefreshingApps {
                ProgressView()
                    .controlSize(.small)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Updating portable app cache")
                        .font(.system(size: 12, weight: .semibold))
                    Text("Fetching the newest binary for each OS and architecture")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                Spacer()
            } else {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 18))
                    .foregroundStyle(.green)
                VStack(alignment: .leading, spacing: 2) {
                    Text(service.newReleaseCount > 0 ? "Work queued" : "Watching for new firmware")
                        .font(.system(size: 12, weight: .semibold))
                    Text(idleDetail)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(Color.primary.opacity(0.035))
    }

    private var idleDetail: String {
        if let date = service.lastRefresh {
            return "Last checked \(date.formatted(date: .omitted, time: .shortened))"
        }
        return "Waiting for the first check"
    }

    private var stats: some View {
        HStack(spacing: 16) {
            metric("\(service.mirrors.count)", "MIRRORS")
            metric("\(service.newReleaseCount)", "RELEASES WAITING", color: service.newReleaseCount > 0 ? .orange : .primary)
            metric("\(service.portableApps.count)", "APPS CACHED", color: service.portableApps.isEmpty ? .secondary : .primary)
            Spacer()
            Toggle(isOn: $service.autoSync) {
                Text("Auto-run")
                    .font(.system(size: 10, weight: .medium))
            }
            .toggleStyle(.switch)
            .controlSize(.mini)
            .help("Automatically pack and upload missing releases")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private func metric(_ value: String, _ label: String, color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value)
                .font(.system(size: 17, weight: .semibold, design: .rounded))
                .foregroundStyle(color)
                .monospacedDigit()
            Text(label)
                .font(.system(size: 7.5, weight: .bold))
                .tracking(0.6)
                .foregroundStyle(.tertiary)
        }
    }

    private var activityHeader: some View {
        HStack {
            Text("LIVE ACTIVITY")
                .font(.system(size: 9, weight: .bold))
                .tracking(0.8)
                .foregroundStyle(.secondary)
            Spacer()
            if service.queueCount > 0 {
                Text("\(service.queueCount) queued")
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 3)
        .padding(.bottom, 4)
    }

    private var activityFeed: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if service.activity.isEmpty {
                        VStack(spacing: 7) {
                            Image(systemName: "waveform.path.ecg")
                                .font(.system(size: 22, weight: .light))
                                .foregroundStyle(.tertiary)
                            Text("Activity appears here as the app checks, downloads, packs and uploads.")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                                .frame(maxWidth: 300)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.top, 30)
                    }
                    ForEach(service.activity.suffix(250)) { line in
                        ActivityRow(line: line)
                            .id(line.id)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 8)
            }
            .onChange(of: service.activity.count) { _ in
                if let last = service.activity.last {
                    withAnimation(.easeOut(duration: 0.12)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Button {
                service.syncAllNew()
            } label: {
                Label("Pack missing", systemImage: "shippingbox.and.arrow.backward")
                    .font(.system(size: 11, weight: .semibold))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(
                !service.canSync ||
                (service.newReleaseCount == 0 && service.mirrorsNeedingAttention == 0)
            )

            Button {
                service.refreshNow()
            } label: {
                Label("Check", systemImage: "arrow.clockwise")
                    .font(.system(size: 11))
            }
            .buttonStyle(.borderless)
            .disabled(service.isRefreshing)

            Spacer()

            if service.isRefreshingApps {
                ProgressView().controlSize(.mini)
            } else if service.autoSync {
                Label("Live", systemImage: "dot.radiowaves.left.and.right")
                    .font(.system(size: 9.5, weight: .medium))
                    .foregroundStyle(.green)
            }

            Button("Quit") {
                service.shutdown()
                NSApplication.shared.terminate(nil)
            }
            .buttonStyle(.borderless)
            .font(.system(size: 10))
            .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
    }
}

private struct ActivityRow: View {
    let line: ActivityLine

    var body: some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: symbol)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 13, height: 13)
                .padding(.top, 1)
            Text(line.text)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(color == .secondary ? Color.secondary : Color.primary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(line.date, format: .dateTime.hour().minute())
                .font(.system(size: 8.5, design: .monospaced))
                .foregroundStyle(.tertiary)
                .padding(.top, 1)
        }
        .padding(.vertical, 3.5)
        .padding(.horizontal, 4)
        .background(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .fill(background)
        )
    }

    private var color: Color {
        switch line.kind {
        case .info: return .secondary
        case .success: return .green
        case .warning: return .orange
        case .error: return .red
        }
    }

    private var symbol: String {
        switch line.kind {
        case .info: return "circle.fill"
        case .success: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.circle.fill"
        }
    }

    private var background: Color {
        switch line.kind {
        case .warning: return Color.orange.opacity(0.06)
        case .error: return Color.red.opacity(0.07)
        default: return .clear
        }
    }
}
