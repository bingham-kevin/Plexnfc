import Foundation
import Network

/// Finds Movie Book servers on the home network: plexnfc announces itself with
/// Bonjour as `_moviebook._tcp`, with its address in the TXT record.
@MainActor
final class ServerBrowser: ObservableObject {
    struct Server: Identifiable, Hashable, Sendable {
        let name: String
        let address: String  // e.g. 192.168.1.20:8080
        var id: String { address }
    }

    @Published private(set) var servers: [Server] = []
    private var browser: NWBrowser?

    func start() {
        guard browser == nil else { return }
        let browser = NWBrowser(for: .bonjourWithTXTRecord(type: "_moviebook._tcp", domain: nil), using: .tcp)
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            // The same server can show up once per network interface
            let found = Set(results.compactMap(Self.server)).sorted { $0.name < $1.name }
            Task { @MainActor in self?.servers = found }
        }
        browser.start(queue: .main)
        self.browser = browser
    }

    func stop() {
        browser?.cancel()
        browser = nil
    }

    nonisolated private static func server(_ result: NWBrowser.Result) -> Server? {
        guard case .bonjour(let txt) = result.metadata, var address = txt["url"],
              case .service(let name, _, _, _) = result.endpoint else { return nil }
        if let scheme = address.range(of: "http://") { address.removeSubrange(scheme) }
        while address.hasSuffix("/") { address.removeLast() }
        return Server(name: name, address: address)
    }
}
