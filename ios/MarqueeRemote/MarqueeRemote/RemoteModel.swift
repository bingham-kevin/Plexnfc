import Foundation
import UIKit

/// A player taps can go to, as the server lists it.
struct Player: Decodable, Identifiable, Hashable {
    let id: String    // what the server's ?client= takes, e.g. "app:Living Room"
    let name: String
    let kind: String  // Marquee, Jellyfin, Plex or Home Assistant
}

/// What's playing on the chosen player, from the server's /now.
struct NowPlaying: Decodable, Equatable {
    let state: String  // idle, starting, playing, paused or error
    let title: String?
    let key: String?
    let player: String?
    let position: Double?
    let duration: Double?
    let message: String?

    var isActive: Bool { ["starting", "playing", "paused"].contains(state) }
}

private struct PlayerList: Decodable {
    let players: [Player]
    let `default`: String
    let selected: String?
    let name: String
}

/// Pairing with the server, the player this phone's taps go to, and what's playing there.
@MainActor
final class RemoteModel: ObservableObject {
    enum Stage: Equatable {
        case welcome
        case pairing(address: String, code: String)
        case connected
    }

    @Published private(set) var stage: Stage
    @Published private(set) var connection: Connection?
    @Published var setupError: String?

    @Published private(set) var players: [Player] = []
    @Published private(set) var defaultPlayer = ""
    @Published private(set) var selected: String?
    @Published private(set) var now: NowPlaying?
    @Published var error: String?

    private var pairingTask: Task<Void, Never>?
    private var api: ServerAPI? { connection.map(ServerAPI.init) }

    init() {
        if let saved = Connection.load() {
            connection = saved
            stage = .connected
        } else {
            stage = .welcome
        }
    }

    var savedAddress: String { UserDefaults.standard.string(forKey: "serverAddress") ?? "" }

    /// "iPhone" or "iPad", for the words on screen.
    var deviceWord: String { UIDevice.current.userInterfaceIdiom == .pad ? "iPad" : "iPhone" }

    var defaultPlayerName: String {
        players.first { $0.id.caseInsensitiveCompare(defaultPlayer) == .orderedSame }?.name ?? defaultPlayer
    }

    // MARK: - Pairing

    /// Ask the server to pair, show the code, and wait for it to be approved in the server's panel.
    func pair(address: String) {
        setupError = nil
        UserDefaults.standard.set(address, forKey: "serverAddress")
        pairingTask?.cancel()
        let name = deviceWord
        pairingTask = Task {
            do {
                let started = try await Pairing.start(address: address, name: name)
                stage = .pairing(address: address, code: started.code)
                while !Task.isCancelled {
                    try await Task.sleep(for: .seconds(2))
                    let result = try await Pairing.check(address: address, pairID: started.pair_id)
                    if result.status == "approved", let secret = result.secret {
                        let connection = Connection(address: address, name: result.name ?? name, secret: secret)
                        connection.save()
                        self.connection = connection
                        stage = .connected
                        await refresh()
                        return
                    }
                    if result.status == "expired" {
                        setupError = "The code expired before it was approved. Try again."
                        stage = .welcome
                        return
                    }
                }
            } catch is CancellationError {
            } catch {
                setupError = "Couldn't reach a Movie Book server at \(address). Check the address, and that this \(name) is on the home Wi-Fi."
                stage = .welcome
            }
        }
    }

    func cancelPairing() {
        pairingTask?.cancel()
        stage = .welcome
    }

    /// Forget the server (and the pairing) and go back to the welcome screen.
    func disconnect() {
        Connection.forget()
        connection = nil
        players = []
        selected = nil
        now = nil
        stage = .welcome
    }

    // MARK: - Players

    /// The players, and this phone's choice. Also how the server learns this phone's
    /// current Wi-Fi address, which is how it recognizes the phone's taps.
    func refresh() async {
        guard let api else { return }
        do {
            let list = try await api.get("phone/players", as: PlayerList.self)
            players = list.players
            defaultPlayer = list.default
            selected = list.selected
            error = nil
        } catch ServerAPI.Failure.notPaired {
            disconnect()
            setupError = "The server no longer knows this \(deviceWord). Pair it again."
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Send this phone's taps to `player`, or to the server's default with nil.
    func choose(_ player: String?) async {
        guard let api else { return }
        let previous = selected
        selected = player  // show it straight away
        do {
            _ = try await api.request("phone/player", method: "POST", body: ["player": player ?? NSNull()])
            now = nil
            await updateNow()
        } catch {
            selected = previous
            self.error = error.localizedDescription
        }
    }

    // MARK: - Now playing

    private func path(_ base: String, _ extra: [URLQueryItem] = []) -> String {
        var parts = URLComponents()
        parts.path = base
        let items = (selected.map { [URLQueryItem(name: "client", value: $0)] } ?? []) + extra
        parts.queryItems = items.isEmpty ? nil : items
        return parts.string ?? base
    }

    func updateNow() async {
        guard let api else { return }
        now = try? await api.get(path("now"), as: NowPlaying.self)
    }

    /// pause, play, stop, toggle, or seek (with seconds).
    func control(_ action: String, by seconds: Int? = nil) async {
        guard let api else { return }
        do {
            let extra = seconds.map { [URLQueryItem(name: "by", value: String($0))] } ?? []
            _ = try await api.request(path(action, extra))
            try? await Task.sleep(for: .milliseconds(600))  // let the player catch up
            await updateNow()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func posterURL(_ key: String) -> URL? {
        api?.url("poster/\(key)?w=200&h=300")
    }
}
