import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: RemoteModel

    var body: some View {
        switch model.stage {
        case .welcome:
            WelcomeView()
        case .pairing(let address, let code):
            PairingView(address: address, code: code)
        case .connected:
            PlayersView()
        }
    }
}

// MARK: - Setup

private struct WelcomeView: View {
    @EnvironmentObject private var model: RemoteModel
    @StateObject private var browser = ServerBrowser()
    @State private var address = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(spacing: 14) {
                        Image("Marquee").resizable().scaledToFit().frame(height: 120)
                            .clipShape(RoundedRectangle(cornerRadius: 16))
                            .accessibilityHidden(true)
                        Text("Choose which TV the movie book's tags play on when you tap them with this \(model.deviceWord).")
                            .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
                }

                Section {
                    if browser.servers.isEmpty {
                        HStack(spacing: 12) {
                            ProgressView()
                            Text("Looking for Movie Book servers…").foregroundStyle(.secondary)
                        }
                    }
                    ForEach(browser.servers) { server in
                        Button {
                            model.pair(address: server.address)
                        } label: {
                            LabeledContent(server.name, value: server.address)
                        }
                    }
                } header: {
                    Text("On this network")
                }

                Section {
                    TextField("192.168.1.20:8080", text: $address)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit(connect)
                    Button("Connect", action: connect)
                        .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                } header: {
                    Text("Or enter its address")
                } footer: {
                    if let error = model.setupError {
                        Text(error).foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Marquee Remote")
        }
        .onAppear {
            if address.isEmpty { address = model.savedAddress }
            browser.start()
        }
        .onDisappear { browser.stop() }
    }

    private func connect() {
        guard !address.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        model.pair(address: address)
    }
}

private struct PairingView: View {
    @EnvironmentObject private var model: RemoteModel
    let address: String
    let code: String

    private var spacedCode: String { code.map(String.init).joined(separator: " ") }

    var body: some View {
        VStack(spacing: 28) {
            Spacer()
            Text("Pair this \(model.deviceWord)").font(.largeTitle.bold())
            Text("Open your Movie Book server's control panel (Marquee Server on the Mac, or the Movie Book panel in Home Assistant), name this \(model.deviceWord), and approve this code.")
                .foregroundStyle(.secondary).multilineTextAlignment(.center)
            Text(spacedCode)
                .font(.system(size: 56, weight: .bold, design: .monospaced))
                .minimumScaleFactor(0.5)
                .lineLimit(1)
                .textSelection(.enabled)
                .accessibilityLabel("Pairing code \(spacedCode)")
            HStack(spacing: 10) {
                ProgressView()
                Text("Waiting for approval from \(address)…").foregroundStyle(.secondary)
            }
            .font(.callout)
            Spacer()
            Button("Cancel", role: .cancel) { model.cancelPairing() }
        }
        .padding(32)
        .frame(maxWidth: 600)
    }
}

// MARK: - Choosing a player

private struct PlayersView: View {
    @EnvironmentObject private var model: RemoteModel
    @Environment(\.scenePhase) private var scenePhase
    @State private var showSettings = false

    /// Players grouped by kind, Marquee (Apple TVs, iPhones, iPads) first.
    private var groups: [(kind: String, players: [Player])] {
        let order = ["Marquee", "Jellyfin", "Plex", "Home Assistant"]
        return Dictionary(grouping: model.players, by: \.kind)
            .map { (kind: $0.key, players: $0.value) }
            .sorted { (order.firstIndex(of: $0.kind) ?? 99, $0.kind) < (order.firstIndex(of: $1.kind) ?? 99, $1.kind) }
    }

    var body: some View {
        NavigationStack {
            List {
                if let now = model.now, now.isActive {
                    NowPlayingSection(now: now)
                }
                Section {
                    PlayerRow(title: "Default", subtitle: model.defaultPlayerName.isEmpty ? nil : model.defaultPlayerName,
                              isSelected: model.selected == nil) {
                        Task { await model.choose(nil) }
                    }
                } header: {
                    Text("Taps from this \(model.deviceWord) play on")
                } footer: {
                    Text("Default follows the server's setting, like everyone else's taps.")
                }
                ForEach(groups, id: \.kind) { group in
                    Section(group.kind) {
                        ForEach(group.players) { player in
                            PlayerRow(title: player.name, subtitle: nil,
                                      isSelected: model.selected?.caseInsensitiveCompare(player.id) == .orderedSame) {
                                Task { await model.choose(player.id) }
                            }
                        }
                    }
                }
                if let error = model.error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Marquee Remote")
            .refreshable { await model.refresh() }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showSettings = true } label: { Label("Settings", systemImage: "gearshape") }
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
        }
        // While open: keep the server's note of this phone's address fresh, and what's playing
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            await model.refresh()
            while !Task.isCancelled {
                await model.updateNow()
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }
}

private struct PlayerRow: View {
    let title: String
    let subtitle: String?
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).foregroundStyle(.primary)
                    if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                if isSelected {
                    Image(systemName: "checkmark").fontWeight(.semibold).foregroundStyle(.tint)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)  // names in the normal text color; only the checkmark is tinted
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

private struct NowPlayingSection: View {
    @EnvironmentObject private var model: RemoteModel
    let now: NowPlaying

    var body: some View {
        Section("Now playing") {
            HStack(alignment: .top, spacing: 16) {
                AsyncImage(url: now.key.flatMap(model.posterURL)) { image in
                    image.resizable().scaledToFill()
                } placeholder: {
                    Color.secondary.opacity(0.15)
                }
                .frame(width: 70, height: 105)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .accessibilityHidden(true)

                VStack(alignment: .leading, spacing: 6) {
                    Text(now.title ?? "Starting…").font(.headline)
                    if let player = now.player {
                        Text(player).font(.caption).foregroundStyle(.secondary)
                    }
                    if let duration = now.duration, duration > 0 {
                        ProgressView(value: min(now.position ?? 0, duration), total: duration)
                        Text("\(time(now.position ?? 0)) of \(time(duration))")
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    } else if now.state == "starting" {
                        Text("Starting").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            HStack {
                control("gobackward.30", "Back 30 seconds") { await model.control("seek", by: -30) }
                Spacer()
                control(now.state == "paused" ? "play.fill" : "pause.fill",
                        now.state == "paused" ? "Play" : "Pause") { await model.control("toggle") }
                Spacer()
                control("goforward.30", "Forward 30 seconds") { await model.control("seek", by: 30) }
                Spacer()
                control("stop.fill", "Stop") { await model.control("stop") }
            }
            .font(.title2)
            .buttonStyle(.borderless)
            .padding(.horizontal, 12)
        }
    }

    private func control(_ symbol: String, _ label: String, _ action: @escaping () async -> Void) -> some View {
        Button { Task { await action() } } label: { Label(label, systemImage: symbol).labelStyle(.iconOnly) }
    }

    private func time(_ seconds: Double) -> String {
        let s = Int(seconds)
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
                         : String(format: "%d:%02d", s / 60, s % 60)
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var model: RemoteModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                if let connection = model.connection {
                    Section("Movie Book server") {
                        LabeledContent("Address", value: connection.address)
                        LabeledContent("This \(model.deviceWord)", value: connection.name)
                        Button("Disconnect and Forget Server", role: .destructive) {
                            model.disconnect()
                            dismiss()
                        }
                    }
                }
                Section("How taps find this \(model.deviceWord)") {
                    Text("A tapped tag opens in Safari, so the server recognizes this \(model.deviceWord) by its Wi-Fi address. Open Marquee Remote after joining the home Wi-Fi, or if taps play in the wrong room.")
                    Text("In Settings → Wi-Fi → your network, set Private Wi-Fi Address to Fixed, so the address doesn't change.")
                }
                .foregroundStyle(.secondary)
                Section("Privacy") {
                    Text("Marquee Remote talks only to your own server on your home network. It collects no data and has no accounts, ads or tracking.")
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}
