import SwiftUI

/// Plex sign-in, where tags point and where they play.
struct ServerStatusView: View {
    let model: PanelModel
    let overview: Overview

    var body: some View {
        PanelSection("Server") {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 18, verticalSpacing: 10) {
                GridRow {
                    Text("Plex").foregroundStyle(.secondary)
                    HStack {
                        if overview.plex.signedIn {
                            Pill(text: "Signed in", tone: .good)
                            if overview.plex.signedInHere && overview.canManage {
                                Button("Sign Out") {
                                    guard confirm("Sign out of Plex?", detail: "Tags stop working until you sign in again.",
                                                  button: "Sign Out") else { return }
                                    Task {
                                        await model.perform("Signed out of Plex") { try await model.api.post("plex/logout") }
                                        model.libraryChanged()
                                    }
                                }
                                .buttonStyle(.link)
                            }
                        } else {
                            Pill(text: "Not signed in", tone: .bad)
                            if overview.canManage { PlexSignInButton(model: model) }
                        }
                        Button("Sources…") { model.selection = .sources }.buttonStyle(.link)
                    }
                }
                GridRow {
                    Text("Tags point at").foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        CodeText(overview.tagAddress)
                        if overview.tagAddressIsLocal {
                            ErrorText("That's this Mac's own name for itself, which phones can't reach. Set the tag "
                                      + "address in Settings to a name or address phones can reach.")
                        }
                    }
                }
                GridRow {
                    Text("Taps play on").foregroundStyle(.secondary)
                    if overview.defaultPlayerLabel.isEmpty {
                        ErrorText("No player chosen yet (choose one in Settings)")
                    } else {
                        Text(overview.defaultPlayerLabel)
                    }
                }
            }
        }
    }
}

struct PlexSignInButton: View {
    let model: PanelModel
    var body: some View {
        if model.signingIn {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Sign in on the plex.tv page that opened…").foregroundStyle(.secondary)
            }
        } else {
            Button("Sign In with Plex") { Task { await model.signInToPlex() } }
                .buttonStyle(.borderedProminent)
        }
    }
}

/// Each Marquee player, with what it's playing and its controls.
struct NowPlayingView: View {
    let model: PanelModel
    let overview: Overview

    var body: some View {
        let players = overview.devices.filter { $0.kind == .tv }
        PanelSection("Now playing") {
            if players.isEmpty {
                Text("No Marquee players yet. Pair one under Devices.").foregroundStyle(.secondary)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 340), spacing: 12)], alignment: .leading, spacing: 12) {
                    ForEach(players) { PlayerCard(model: model, device: $0, canControl: overview.canManage) }
                }
            }
        }
    }
}

private struct PlayerCard: View {
    let model: PanelModel
    let device: Device
    let canControl: Bool
    @State private var busy = false
    @State private var since = (at: Date(), position: 0.0)

    var body: some View {
        let status = device.status
        HStack(alignment: .top, spacing: 14) {
            if let key = status?.key {
                Poster(url: model.poster(key, 200, 300), width: 90)
            } else {
                RoundedRectangle(cornerRadius: 8).fill(.quaternary).frame(width: 90, height: 135)
                    .overlay(Image(systemName: "film").font(.title).foregroundStyle(.secondary))
            }
            VStack(alignment: .leading, spacing: 6) {
                Text(device.name).font(.headline)
                if let status {
                    Text(status.title ?? status.key ?? "").font(.title3).lineLimit(2)
                    Group {
                        if let source = status.source {
                            Text("\(status.state == "paused" ? "Paused" : "Playing") from **\(source)**")
                        } else {
                            Text(status.state == "paused" ? "Paused" : "Playing")
                        }
                    }
                    .foregroundStyle(.secondary)
                    if let duration = status.duration, duration > 0 {
                        TimelineView(.periodic(from: .now, by: 1)) { context in
                            let position = position(status, at: context.date)
                            VStack(alignment: .leading, spacing: 2) {
                                ProgressView(value: min(position, duration), total: duration)
                                Text("\(clock(position)) / \(clock(duration))").font(.caption).monospacedDigit()
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    if canControl {
                        HStack {
                            if status.state == "playing" {
                                Button { control("pause") } label: { Label("Pause", systemImage: "pause.fill") }
                            } else {
                                Button { control("play") } label: { Label("Play", systemImage: "play.fill") }
                            }
                            Button { control("stop") } label: { Label("Stop", systemImage: "stop.fill") }
                        }
                        .disabled(busy)
                    }
                } else {
                    Text("Nothing playing, or Marquee is closed").foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
        .onChange(of: status?.position, initial: true) { _, position in since = (Date(), position ?? 0) }
    }

    /// Marquee reports every few seconds; between reports a playing movie moves on by itself.
    private func position(_ status: PlayerStatus, at date: Date) -> Double {
        let moved = status.state == "playing" ? max(0, date.timeIntervalSince(since.at)) : 0
        return min(since.position + moved, status.duration ?? .infinity)
    }

    private func control(_ action: String) {
        busy = true
        Task {
            await model.perform { try await model.api.post("control", ["player": "app:\(device.name)", "action": action]) }
            busy = false
        }
    }
}
