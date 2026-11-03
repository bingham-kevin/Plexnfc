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

    /// Ask the server to pair, show the code, and wait for it to be approved in the server's panel.
    func pair(address: String) {
        setupError = nil
        UserDefaults.standard.set(address, forKey: "serverAddress")  // so it's still there after an error
        pairingTask?.cancel()
        let name = UIDevice.current.name
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
                        UserDefaults.standard.set(false, forKey: "demoMode")
                        self.connection = connection
                        stage = .connected
                        apply()
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
