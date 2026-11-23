import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Every movie's tag address: search, sort, copy, link a card, match, or try one on a player.
struct TagsView: View {
    let model: PanelModel
    let overview: Overview
    @State private var movies: [Movie] = []
    @State private var controls: [Movies.Control] = []
    @State private var loading = true
    @State private var error: String?
    @State private var search = ""
    @State private var typedCollection = ""
    @State private var collection = ""
    @State private var player = ""
    @State private var players: [Player] = []
    @State private var sortOrder = [KeyPathComparator(\Movie.title, comparator: .localizedStandard)]
    @State private var selection: Movie.ID?
    @State private var editing: Movie?
    @State private var showControls = false

    private var shown: [Movie] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        let found = q.isEmpty ? movies : movies.filter {
            "\($0.title) \($0.yearText) \($0.key) \($0.source)".lowercased().contains(q)
        }
        return found.sorted(using: sortOrder)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            toolbar
            if let error { ErrorText(error) }
            Table(shown, selection: $selection, sortOrder: $sortOrder) {
                TableColumn("") { movie in
                    Poster(url: model.poster(movie.key, 80, 120), width: 26)
                }
                .width(30)
                TableColumn("Title", value: \.title, comparator: .localizedStandard) { movie in
                    Text(movie.title).bold()
                }
                .width(min: 160, ideal: 260)
                TableColumn("Year", value: \.sortYear) { movie in Text(movie.yearText) }
                    .width(50)
                TableColumn("Source", value: \.source) { movie in
                    Text(movie.source).foregroundStyle(.secondary)
                }
                .width(min: 80, ideal: 150)
                TableColumn("Tag address") { movie in CodeText(movie.url) }
                    .width(min: 160, ideal: 260)
                TableColumn("") { movie in actions(movie) }
                    .width(min: 290, ideal: 300)
            }
            .contextMenu(forSelectionType: Movie.ID.self) { ids in
                if let movie = movies.first(where: { ids.contains($0.id) }) { menu(movie) }
            }
            .overlay {
                if loading && movies.isEmpty { ProgressView("Loading the library…") }
            }
            footer
        }
        .padding(16)
        .task(id: "\(collection)|\(model.libraryVersion)") { await load() }
        .task {
            if let found: Players = try? await model.api.get("players") { players = found.players }
        }
        .sheet(item: $editing) { movie in EditMovieSheet(model: model, movie: movie) }
    }

    private var toolbar: some View {
        HStack(spacing: 10) {
            TextField("Search titles", text: $search)
                .textFieldStyle(.roundedBorder)
                .frame(maxWidth: 240)
            TextField("Collection (all movies)", text: $typedCollection)
                .textFieldStyle(.roundedBorder)
                .frame(maxWidth: 200)
                .onSubmit { collection = typedCollection.trimmingCharacters(in: .whitespaces) }
            Button("Show") { collection = typedCollection.trimmingCharacters(in: .whitespaces) }
            Spacer()
            if overview.canManage {
                Picker("Try on", selection: $player) {
                    Text("\(overview.defaultPlayerLabel.isEmpty ? "The default player" : overview.defaultPlayerLabel) (default)")
                        .tag("")
                    ForEach(players.filter { $0.id != overview.defaultPlayer }) { Text("\($0.name) (\($0.kind))").tag($0.id) }
                }
                .frame(maxWidth: 280)
            }
            Button { Task { await downloadCSV() } } label: { Label("Download CSV", systemImage: "square.and.arrow.down") }
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("\(shown.count) of \(movies.count) movies").foregroundStyle(.secondary)
                Spacer()
                Text("Write each address onto the tag stuck to that movie's poster. Add ?resume=1 to carry on where it was left off.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            DisclosureGroup("Control tags", isExpanded: $showControls) {
                Text("For the back cover: they act on whatever the default player is playing.")
                    .font(.caption).foregroundStyle(.secondary)
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                    ForEach(controls) { control in
                        GridRow {
                            Text(control.action.capitalized).bold()
                            CodeText(control.url)
                            Button("Copy") { copyToPasteboard(control.url); model.say("Copied") }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private func actions(_ movie: Movie) -> some View {
        HStack(spacing: 6) {
            Button("Copy") { copyToPasteboard(movie.url); model.say("Copied") }
            if overview.canManage {
                Button(movie.cards.isEmpty ? "Link card" : "✓ Card") { linkCard(movie) }
                    .help(movie.cards.isEmpty ? "Link a blank card with a Movie Remote" : "Has card \(movie.cards.joined(separator: ", "))")
                if !movie.files.isEmpty {
                    Button(movie.key.hasPrefix("t-") ? "Match…" : "Edit") { editing = movie }
                        .help("Match it with TMDB, or change its details")
                }
                Button { play(movie) } label: { Label("Try", systemImage: "play.fill") }
                    .buttonStyle(.borderedProminent)
                    .help("Start it, as tapping the tag would")
            }
        }
        .controlSize(.small)
    }

    @ViewBuilder private func menu(_ movie: Movie) -> some View {
        Button("Copy Tag Address") { copyToPasteboard(movie.url); model.say("Copied") }
        if overview.canManage {
            Button("Try on \(player.isEmpty ? "the Default Player" : players.first { $0.id == player }?.name ?? player)") { play(movie) }
            Button("Link a Card…") { linkCard(movie) }
            if !movie.files.isEmpty { Button("Match or Edit…") { editing = movie } }
        }
    }

    private func load() async {
        loading = true
        do {
            let answer: Movies = try await model.api.get("movies", ["collection": collection])
            movies = answer.movies
            controls = answer.controls
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    private func play(_ movie: Movie) {
        Task {
            await model.perform("Starting \(movie.title)") {
                try await model.api.post("play", ["key": movie.key, "player": player.isEmpty ? NSNull() : player])
            }
        }
    }

    /// One movie's card: a scan of just that movie, on the Cards tab.
    private func linkCard(_ movie: Movie) {
        Task {
            let worked = await model.perform {
                try await model.api.post("cards/scan", ["movies": [["key": movie.key, "title": movie.title,
                                                                     "year": movie.year ?? NSNull()] as [String: Any]]])
            }
            if worked { model.selection = .cards }
        }
    }

    private func downloadCSV() async {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = collection.isEmpty ? "tags.csv" : "tags-\(collection).csv"
        panel.allowedContentTypes = [.commaSeparatedText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let data = try await model.api.raw("tags.csv", ["collection": collection])
            try data.write(to: url)
            model.say("Saved \(url.lastPathComponent)")
        } catch {
            model.say(error.localizedDescription, error: true)
        }
    }
}
