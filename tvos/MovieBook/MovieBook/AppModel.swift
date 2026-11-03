import Foundation
import UIKit

/// Where the app is: setting up, browsing the demo library, or connected to a server.
@MainActor
final class AppModel: ObservableObject {
    enum Stage: Equatable {
        case welcome
        case pairing(address: String, code: String)
        case demo
        case connected
    }

    @Published private(set) var stage: Stage
    @Published private(set) var connection: Connection?
    @Published var setupError: String?

    let player = PlayerModel()
    let library = LibraryModel()
    private var pairingTask: Task<Void, Never>?
    private var expectedCheck: Task<Void, Never>?

    init() {
        if let saved = Connection.load() {
            connection = saved
            stage = .connected
        } else {
            stage = UserDefaults.standard.bool(forKey: "demoMode") ? .demo : .welcome
        }
        apply()
    }

    var savedAddress: String { UserDefaults.standard.string(forKey: "serverAddress") ?? "" }

    // MARK: - Setup

    func startDemo() {
        UserDefaults.standard.set(true, forKey: "demoMode")
        stage = .demo
        apply()
    }

    /// While Marquee is open, pair with a Movie Book server that's expecting this Apple TV,
    /// if one is: one that was just paired with it (with the PIN on the TV) and then opened
    /// Marquee. So pairing the Apple TV on the server is all it takes, even when Marquee is
    /// connected to another server. It checks every few seconds, because a server opening
    /// Marquee when it's already open doesn't tell the app anything.
    func startPairingWithExpectingServers() {
        expectedCheck?.cancel()
        expectedCheck = Task {
            while !Task.isCancelled {
                if case .pairing = stage {} else if let server = await expectingServer() {  // not mid-pairing
                    player.stop()
                    pair(address: server, expected: true)
                }
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }

    func stopPairingWithExpectingServers() {
        expectedCheck?.cancel()
        expectedCheck = nil
    }

    private func expectingServer() async -> String? {
        let browser = ServerBrowser()
        browser.start()
        try? await Task.sleep(for: .seconds(2))  // let Bonjour answer
        browser.stop()
        for server in browser.servers where await Pairing.isExpected(address: server.address) {
            return server.address
        }
        return nil
    }

    /// Ask the server to pair, show the code, and wait for it to be approved in the server's panel.
    /// A server that's `expected` this Apple TV approves straight away, so the code only shows if it didn't.
    func pair(address: String, expected: Bool = false) {
        setupError = nil
        UserDefaults.standard.set(address, forKey: "serverAddress")  // so it's still there after an error
        pairingTask?.cancel()
        let name = UIDevice.current.name
        pairingTask = Task {
            do {
                let started = try await Pairing.start(address: address, name: name)
                if !expected { stage = .pairing(address: address, code: started.code) }
                while !Task.isCancelled {
                    try await Task.sleep(for: .seconds(stage == .pairing(address: address, code: started.code) ? 2 : 0.5))
                    let result = try await Pairing.check(address: address, pairID: started.pair_id)
                    if result.status == "approved", let secret = result.secret {
                        let connection = Connection(address: address, name: result.name ?? name, secret: secret)
                        connection.save()
                        UserDefaults.standard.set(false, forKey: "demoMode")
                        self.connection = connection
                        stage = .connected
                        apply()
                        return
                    }
                    if result.status == "pending" {
                        stage = .pairing(address: address, code: started.code)
                    }
                    if result.status == "expired" {
                        setupError = "The code expired before it was approved. Try again."
                        stage = .welcome
                        return
                    }
                }
            } catch is CancellationError {
            } catch {
                setupError = "Couldn't reach a Movie Book server at \(address). Check the address and that the Apple TV is on the same network."
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
        UserDefaults.standard.set(false, forKey: "demoMode")
        connection = nil
        player.stop()
        stage = .welcome
        apply()
    }

    private func apply() {
        let api = connection.map(ServerAPI.init)
        player.api = api
        library.use(api)
    }
}
