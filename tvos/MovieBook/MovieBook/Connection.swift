import Foundation
import Security

/// Where the Movie Book server is, and this Apple TV's pairing with it.
///
/// The address and name are ordinary settings; the pairing secret lives in the
/// Keychain, because it unlocks stream addresses that include the server's
/// media sign-in.
struct Connection: Equatable {
    var address: String
    var name: String      // this Apple TV, as the server shows it (and taps address it)
    var secret: String

    var baseURL: URL? { Connection.baseURL(for: address) }

    static func baseURL(for address: String) -> URL? {
        var address = address.trimmingCharacters(in: .whitespaces)
        guard !address.isEmpty else { return nil }
        if !address.contains("://") { address = "http://" + address }
        while address.hasSuffix("/") { address.removeLast() }
        return URL(string: address)
    }

    // MARK: - Saved connection

    static func load() -> Connection? {
        let defaults = UserDefaults.standard
        guard let address = defaults.string(forKey: "serverAddress"),
              let name = defaults.string(forKey: "deviceName"),
              let secret = Keychain.read("pairingSecret") else { return nil }
        return Connection(address: address, name: name, secret: secret)
    }

    func save() {
        UserDefaults.standard.set(address, forKey: "serverAddress")
        UserDefaults.standard.set(name, forKey: "deviceName")
        Keychain.write("pairingSecret", secret)
    }

    static func forget() {
        UserDefaults.standard.removeObject(forKey: "deviceName")
        Keychain.delete("pairingSecret")
    }
}

/// Requests to the Movie Book server, signed with this Apple TV's pairing secret.
struct ServerAPI {
    let connection: Connection

    enum Failure: LocalizedError {
        case notPaired, server(String)
        var errorDescription: String? {
            switch self {
            case .notPaired: "This Apple TV isn't paired with the server anymore. Pair it again in Settings."
            case .server(let message): message
            }
        }
    }

    func url(_ path: String) -> URL? {
        connection.baseURL.flatMap { URL(string: path, relativeTo: $0)?.absoluteURL }
    }

    func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> (Data, Int) {
        guard let url = url(path) else { throw Failure.server("The server address isn't valid.") }
        var request = URLRequest(url: url, timeoutInterval: 20)
        request.httpMethod = method
        request.setValue("Bearer \(connection.secret)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw Failure.notPaired }
        if status >= 400 {
            let message = (try? JSONDecoder().decode(ServerMessage.self, from: data))?.message
            throw Failure.server(message ?? "The server answered \(status).")
        }
        return (data, status)
    }

    func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        try JSONDecoder().decode(T.self, from: try await request(path).0)
    }
}

private struct ServerMessage: Decodable { let message: String? }

/// Pairing: ask the server for a code, then wait for someone to approve it in the server's panel.
enum Pairing {
    struct Started: Decodable { let pair_id: String; let code: String }
    struct Result: Decodable { let status: String; let secret: String?; let name: String? }

    static func start(address: String, name: String) async throws -> Started {
        guard let base = Connection.baseURL(for: address),
              let url = URL(string: "tv/pair", relativeTo: base) else {
            throw ServerAPI.Failure.server("That doesn't look like a server address.")
        }
        var request = URLRequest(url: url, timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["name": name])
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw ServerAPI.Failure.server("That address answered, but it isn't a Movie Book server.")
        }
        return try JSONDecoder().decode(Started.self, from: data)
    }

    /// The pairing's state: nil while waiting, the secret once approved.
    static func check(address: String, pairID: String) async throws -> Result {
        guard let base = Connection.baseURL(for: address),
              let url = URL(string: "tv/pair/\(pairID)", relativeTo: base) else {
            throw ServerAPI.Failure.server("That doesn't look like a server address.")
        }
        let (data, response) = try await URLSession.shared.data(from: url)
        if (response as? HTTPURLResponse)?.statusCode == 404 {
            return Result(status: "expired", secret: nil, name: nil)
        }
        return try JSONDecoder().decode(Result.self, from: data)
    }
}

/// A tiny Keychain wrapper for the pairing secret.
enum Keychain {
    private static func query(_ key: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: Bundle.main.bundleIdentifier ?? "Marquee",
         kSecAttrAccount as String: key]
    }

    static func read(_ key: String) -> String? {
        var q = query(key)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ key: String, _ value: String) {
        delete(key)
        var q = query(key)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(q as CFDictionary, nil)
    }

    static func delete(_ key: String) {
        SecItemDelete(query(key) as CFDictionary)
    }
}
