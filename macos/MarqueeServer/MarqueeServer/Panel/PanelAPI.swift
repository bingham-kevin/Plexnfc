import Foundation

// The server's control panel API (/panel/api in plexnfc/server.py), as the web panel uses it.
// The app talks to the server on this Mac, so it can change everything the web panel can.

struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Calls the control panel API of the server running on this Mac.
@MainActor
struct PanelAPI {
    let base: URL  // http://localhost:8080/

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func url(_ path: String, _ query: [String: String] = [:]) -> URL {
        var components = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        let items = query.filter { !$0.value.isEmpty }.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        if !items.isEmpty { components.queryItems = items }
        return components.url!
    }

    /// The address of a movie's poster, sized for where it's shown.
    func poster(_ key: String, _ width: Int, _ height: Int, version: String = "") -> URL {
        url("poster/\(key)", ["w": "\(width)", "h": "\(height)", "v": version])
    }

    func get<T: Decodable>(_ path: String, _ query: [String: String] = [:], as type: T.Type = T.self) async throws -> T {
        try await send(URLRequest(url: url("panel/api/\(path)", query)))
    }

    @discardableResult
    func post<T: Decodable>(_ path: String, _ body: [String: Any] = [:], as type: T.Type = T.self) async throws -> T {
        var request = URLRequest(url: url("panel/api/\(path)"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.timeoutInterval = 120  // finding Apple TVs and reading a folder take a while
        return try await send(request)
    }

    func post(_ path: String, _ body: [String: Any] = [:]) async throws {
        let _: OK = try await post(path, body)
    }

    func raw(_ path: String, _ query: [String: String] = [:]) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(from: url(path, query))
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            throw APIError(message: "The server answered \(http.statusCode)")
        }
        return data
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw APIError(message: "Can't reach the server: \(error.localizedDescription)")
        }
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let message = (try? Self.decoder.decode(OK.self, from: data))?.message
            throw APIError(message: message ?? "The server answered \(http.statusCode)")
        }
        do {
            return try Self.decoder.decode(T.self, from: data)
        } catch {
            throw APIError(message: "The server's answer couldn't be read: \(error)")
        }
    }
}

struct OK: Decodable, Sendable {
    let ok: Bool?
    let message: String?
}

// MARK: - What the API answers

enum DeviceKind: String, Decodable, Sendable, CaseIterable {
    case tv, remote, phone, homeassistant

    var label: String {
        switch self {
        case .tv: "Marquee"
        case .remote: "Movie Remote"
        case .phone: "Marquee Remote"
        case .homeassistant: "Home Assistant"
        }
    }
}

struct PlayerStatus: Decodable, Sendable, Equatable {
    let state: String  // "playing" or "paused"
    let title: String?
    let key: String?
    let source: String?
    let position: Double?
    let duration: Double?
}

struct Device: Decodable, Sendable, Identifiable, Equatable {
    let id: String
    let name: String
    let kind: DeviceKind
    let paired: Double
    let player: String?
    let status: PlayerStatus?
}

struct PairingRequest: Decodable, Sendable, Identifiable, Equatable {
    let pairId: String
    let name: String
    let kind: DeviceKind
    let code: String
    var id: String { pairId }
}

struct LinkedAppleTV: Decodable, Sendable, Identifiable, Equatable {
    let player: String
    let name: String
    let address: String
    let identifier: String
    var id: String { player }
}

struct Overview: Decodable, Sendable, Equatable {
    struct PlexState: Decodable, Sendable, Equatable { let signedIn: Bool; let signedInHere: Bool }
    struct AppleTVs: Decodable, Sendable, Equatable { let available: Bool; let linked: [LinkedAppleTV] }
    let canManage: Bool
    let tagAddress: String
    let tagAddressIsLocal: Bool
    let defaultPlayer: String
    let defaultPlayerLabel: String
    let offTag: Bool
    let plex: PlexState
    let pairingRequests: [PairingRequest]
    let devices: [Device]
    let appleTvs: AppleTVs
}

struct FolderFile: Decodable, Sendable, Hashable {
    let id: String
    let source: String
    let matched: String
}

struct Movie: Decodable, Sendable, Identifiable, Hashable {
    let key: String
    let title: String
    let year: Int?
    let url: String
    let cards: [String]
    let source: String
    let sources: [String]
    let files: [FolderFile]
    var id: String { key }
    var yearText: String { year.map(String.init) ?? "" }
    /// For sorting by year, with movies of no year last
    var sortYear: Int { year ?? Int.max }
}

struct Movies: Decodable, Sendable {
    struct Control: Decodable, Sendable, Identifiable { let action: String; let url: String; var id: String { action } }
    let movies: [Movie]
    let controls: [Control]
}

struct Filters: Decodable, Sendable {
    struct Collection: Decodable, Sendable, Hashable { let title: String; let count: Int? }
    struct SourceName: Decodable, Sendable, Hashable { let id: String; let name: String }
    let collections: [Collection]
    let labels: [String]
    let sources: [SourceName]
}

struct Player: Decodable, Sendable, Identifiable, Hashable {
    let id: String
    let name: String
    let kind: String
}

struct Players: Decodable, Sendable {
    let players: [Player]
    let `default`: String
}

struct FoundAppleTV: Decodable, Sendable, Identifiable {
    let identifier: String
    let name: String
    let address: String
    let linked: String?
    var id: String { identifier }
}

/// What a music card plays: an item from the Music tab (``id``), or, from before, something in
/// Music Assistant (``uri``).
struct MusicItem: Decodable, Sendable, Equatable {
    let id: String?
    let uri: String?
    let mediaType: String?
    let name: String
    let artist: String
    let image: String?

    /// An audiobook from Audiobookshelf
    var isBook: Bool { id?.hasPrefix("abs|") == true }
}

/// How music cards reach Music Assistant: directly, or through the Marquee integration in
/// Home Assistant.
struct MusicStatus: Decodable, Sendable {
    /// A speaker, by its Music Assistant player ID
    struct Speaker: Decodable, Sendable, Identifiable { let id: String; let name: String }
    /// The direct connection's settings (the token itself is never sent)
    struct Assistant: Decodable, Sendable { let url: String; let token: Bool; let connected: Bool; let message: String? }
    let connected: Bool
    let musicAssistant: Bool
    /// "music_assistant", "home_assistant" or nil: which way music goes now
    let via: String?
    let speakers: [Speaker]
    let assistant: Assistant
    let homeAssistant: Bool
}

struct Card: Decodable, Sendable, Identifiable {
    let uid: String
    let key: String
    let title: String
    let linked: Double
    /// A music card: what it plays, and on which Music Assistant speaker
    let music: MusicItem?
    let speakerName: String?
    var id: String { uid }

    /// Which section it belongs to: a movie, music, or an audiobook
    var section: MediaSection { music == nil ? .movies : music!.isBook ? .books : .music }
}

struct ScanMovie: Decodable, Sendable, Equatable {
    let key: String
    let title: String
    let year: Int?
    let music: MusicItem?
    let speakerName: String?
}

struct Scan: Decodable, Sendable {
    struct Last: Decodable, Sendable { let uid: String; let title: String; let key: String; let movedFrom: String?; let at: Double }
    let index: Int
    let total: Int
    let linked: Int
    let current: ScanMovie?
    let next: ScanMovie?
    let done: Bool
    let last: Last?
}

struct CardsState: Decodable, Sendable {
    struct Unknown: Decodable, Sendable { let uid: String; let at: Double }
    let cards: [Card]
    let scan: Scan?
    let unknown: Unknown?
}

struct MatchJob: Decodable, Sendable, Equatable {
    let running: Bool
    let done: Int
    let total: Int
    let matched: Int
    let error: String?
}

struct PlexSource: Decodable, Sendable {
    let enabled: Bool
    let signedIn: Bool
    let library: String
}

struct MovieSource: Decodable, Sendable, Identifiable {
    let id: String
    let kind: String  // "jellyfin" or "folder"
    let name: String
    let error: String?
    let count: Int?
    let url: String?
    let library: String?
    let user: String?
    let path: String?

    var kindName: String { kind == "jellyfin" ? "Jellyfin" : "Folder" }
    var whereText: String {
        if kind == "jellyfin" {
            return [url, library, user].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · ")
        }
        return path ?? ""
    }
}

struct SourcesState: Decodable, Sendable {
    let plex: PlexSource
    let sources: [MovieSource]
    let order: [String]
    let tmdb: Bool
    let matching: [String: MatchJob]
    let count: Int?
}

struct FolderMovie: Decodable, Sendable {
    struct FileInfo: Decodable, Sendable { let title: String?; let year: Int? }
    let id: String
    let key: String
    let source: String
    let path: String
    let title: String
    let year: Int?
    let summary: String
    let collections: [String]
    let labels: [String]
    let ids: [String: String]
    let matched: String
    let edited: Bool
    let file: FileInfo
}

struct TmdbResult: Decodable, Sendable, Identifiable {
    let id: Int
    let title: String
    let year: Int?
    let summary: String
    let poster: String?
}
