import AppKit
import SwiftUI

struct SettingsView: View {
    @ObservedObject var service: MirrorService
    @Binding var isPresented: Bool

    @State private var organization: String
    @State private var manifestURL: String
    @State private var pollInterval: Int
    @State private var autoSync: Bool
    @State private var token = ""

    init(service: MirrorService, isPresented: Binding<Bool>) {
        self.service = service
        _isPresented = isPresented
        _organization = State(initialValue: service.organization)
        _manifestURL = State(initialValue: service.manifestURL)
        _pollInterval = State(initialValue: service.pollInterval)
        _autoSync = State(initialValue: service.autoSync)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 17) {
                    githubSection
                    watchSection
                    portableSection
                }
                .padding(16)
            }
            Divider()
            footer
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Button {
                isPresented = false
            } label: {
                Image(systemName: "chevron.left")
                    .frame(width: 24, height: 24)
            }
            .buttonStyle(.borderless)
            Text("Settings")
                .font(.system(size: 15, weight: .semibold))
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 12)
    }

    private var githubSection: some View {
        VStack(alignment: .leading, spacing: 9) {
            sectionTitle("GITHUB")
            HStack(spacing: 7) {
                Image(systemName: service.auth.hasToken ? "checkmark.shield.fill" : "exclamationmark.shield.fill")
                    .foregroundStyle(service.auth.hasToken ? .green : .orange)
                VStack(alignment: .leading, spacing: 1) {
                    Text(service.auth.hasToken ? "Connected" : "Token required")
                        .font(.system(size: 11, weight: .semibold))
                    Text(service.auth.detail)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            SecureField("Paste a GitHub token", text: $token)
                .textFieldStyle(.roundedBorder)
            HStack {
                Text("Stored in Keychain. Needs repo write and org administration to create mirrors.")
                    .font(.system(size: 9.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                if service.auth.hasToken {
                    Button("Clear") {
                        service.clearToken()
                    }
                    .controlSize(.small)
                }
            }
        }
    }

    private var watchSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle("WATCHING")
            row("Organization") {
                TextField("y1-community", text: $organization)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 190)
            }
            row("Manifest") {
                TextField("https://innioasis.app/slidia_manifest.xml", text: $manifestURL)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 220)
            }
            row("Check every") {
                Picker("", selection: $pollInterval) {
                    Text("30 seconds").tag(30)
                    Text("1 minute").tag(60)
                    Text("2 minutes").tag(120)
                    Text("5 minutes").tag(300)
                    Text("15 minutes").tag(900)
                }
                .labelsHidden()
                .frame(width: 130)
            }
            Toggle("Pack and upload missing releases automatically", isOn: $autoSync)
                .toggleStyle(.switch)
                .controlSize(.small)
            Text("Auto-run reads every manifest product, creates missing mirror repositories, caches the latest portable binaries, then packs missing releases newest-first.")
                .font(.system(size: 9.5))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var portableSection: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                sectionTitle("PORTABLE APPS")
                Spacer()
                if service.isRefreshingApps {
                    ProgressView().controlSize(.mini)
                }
            }
            if service.portableApps.isEmpty {
                Text("The cache is empty. The app downloads the newest asset for each OS and architecture automatically.")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(PortableAppStore.specs, id: \.key) { spec in
                    HStack(spacing: 6) {
                        Image(systemName: service.portableApps[spec.key] == nil ? "circle.dashed" : "checkmark.circle.fill")
                            .font(.system(size: 9))
                            .foregroundStyle(service.portableApps[spec.key] == nil ? Color.secondary : Color.green)
                        Text(spec.label)
                            .font(.system(size: 10))
                        Spacer()
                        if let asset = service.portableApps[spec.key] {
                            Text(asset.tag)
                                .font(.system(size: 9, design: .monospaced))
                                .foregroundStyle(.secondary)
                            Text(asset.sizeText)
                                .font(.system(size: 9, design: .monospaced))
                                .foregroundStyle(.tertiary)
                        } else {
                            Text("not published")
                                .font(.system(size: 9))
                                .foregroundStyle(.tertiary)
                        }
                    }
                }
            }
        }
    }

    private var footer: some View {
        HStack {
            if let message = service.setupMessage {
                Text(message)
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            Button("Cancel") { isPresented = false }
                .controlSize(.small)
            Button("Save & Check") {
                service.applySettings(
                    organization: organization,
                    pollInterval: pollInterval,
                    autoSync: autoSync,
                    manifestURL: manifestURL,
                    token: token
                )
                isPresented = false
            }
            .controlSize(.small)
            .keyboardShortcut(.defaultAction)
            .disabled(organization.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private func sectionTitle(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 9, weight: .bold))
            .tracking(0.8)
            .foregroundStyle(.secondary)
    }

    private func row<Content: View>(_ label: String, @ViewBuilder content: () -> Content) -> some View {
        HStack {
            Text(label)
                .font(.system(size: 11))
            Spacer()
            content()
        }
    }
}
