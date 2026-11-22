import Foundation

/// A movie as the app shows it in rows, search and the player.
struct Movie: Decodable, Identifiable, Hashable {
    let key: String
    let title: String
    let year: Int?
    let summary: String?
    let duration: Int?
    let poster: String?      // a path on the server, or the name of a bundled image (demo)

    var id: String { key }

    /// The demo library's films play these streams directly.
    var demoStream: String? { DemoLibrary.streams[key] }
}

struct MovieRow: Decodable, Identifiable {
    let title: String
    let items: [Movie]
    var id: String { title }
}

/// The server's library: home rows, the full list a page at a time, and search.
@MainActor
final class LibraryModel: ObservableObject {
    @Published private(set) var rows: [MovieRow] = []
    @Published private(set) var allMovies: [Movie] = []
    @Published private(set) var totalMovies = 0
    @Published private(set) var searchResults: [Movie] = []
    @Published private(set) var error: String?
    @Published private(set) var isLoading = false

    private var api: ServerAPI?
    private var searchTask: Task<Void, Never>?

    func use(_ api: ServerAPI?) {
        self.api = api
        rows = api == nil ? DemoLibrary.rows : []
        allMovies = api == nil ? DemoLibrary.movies : []
        totalMovies = allMovies.count
        searchResults = []
        error = nil
    }

    var isDemo: Bool { api == nil }

    func loadHome() async {
        guard let api else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            struct Rows: Decodable { let rows: [MovieRow] }
            rows = try await api.get("tv/library", as: Rows.self).rows.filter { !$0.items.isEmpty }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Loads the next page of the full library when the grid nears its end.
    func loadMoreIfNeeded(after movie: Movie? = nil) async {
        guard let api, !isLoading else { return }
        if let movie, let index = allMovies.firstIndex(of: movie), index < allMovies.count - 20 { return }
        if !allMovies.isEmpty && allMovies.count >= totalMovies { return }
        isLoading = true
        defer { isLoading = false }
        do {
            struct Page: Decodable { let items: [Movie]; let total: Int }
            let page = try await api.get("tv/movies?start=\(allMovies.count)&size=60", as: Page.self)
            allMovies += page.items
            totalMovies = page.total
        } catch {
            self.error = error.localizedDescription
        }
    }

    func search(_ text: String) {
        searchTask?.cancel()
        let query = text.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { searchResults = []; return }
        guard let api else {
            searchResults = DemoLibrary.movies.filter { $0.title.localizedCaseInsensitiveContains(query) }
            return
        }
        searchTask = Task {
            try? await Task.sleep(for: .milliseconds(350))  // wait for typing to pause
            guard !Task.isCancelled else { return }
            struct Results: Decodable { let items: [Movie] }
            let escaped = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
            if let results = try? await api.get("tv/search?q=\(escaped)", as: Results.self), !Task.isCancelled {
                searchResults = results.items
            }
        }
    }
}

/// Free films to try the app with, before (or without) a server.
enum DemoLibrary {
    static let movies: [Movie] = [
        Movie(key: "demo-big-buck-bunny", title: "Big Buck Bunny", year: 2008,
              summary: "A giant rabbit takes on three bullying rodents. An open movie by the Blender Foundation.",
              duration: 596, poster: "DemoBigBuckBunny"),
        Movie(key: "demo-sintel", title: "Sintel (preview)", year: 2010,
              summary: "A lonely young woman searches for the baby dragon she befriended. A short preview of the Blender Foundation's open movie.",
              duration: 10, poster: "DemoSintel"),
        Movie(key: "demo-jellyfish", title: "Jellyfish (preview)", year: nil,
              summary: "A short clip of jellyfish drifting in blue water.", duration: 10, poster: "DemoJellyfish"),
    ]

    static let streams: [String: String] = [
        "demo-big-buck-bunny": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        "demo-sintel": "https://test-videos.co.uk/vids/sintel/mp4/h264/1080/Sintel_1080_10s_1MB.mp4",
        "demo-jellyfish": "https://test-videos.co.uk/vids/jellyfish/mp4/h264/1080/Jellyfish_1080_10s_1MB.mp4",
    ]

    static let rows = [
        MovieRow(title: "Open Movies", items: Array(movies.prefix(1))),
        MovieRow(title: "Short Previews", items: Array(movies.suffix(2))),
    ]

    static let credits = """
        Big Buck Bunny and Sintel are open movies by the Blender Foundation (www.blender.org), \
        licensed under Creative Commons Attribution 3.0. Streams: Big Buck Bunny via Mux test streams, \
        previews via test-videos.co.uk. Demo posters were made for this app.
        """
}
