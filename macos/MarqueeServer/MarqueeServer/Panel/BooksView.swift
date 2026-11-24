import SwiftUI

// The Books section: audiobooks from Audiobookshelf, played on the same speakers as music,
// picked up where you left off. Same API as the web panel's Books pages.

struct BooksState: Decodable, Sendable {
    struct Audiobookshelf: Decodable, Sendable {
        struct Library: Decodable, Sendable, Identifiable { let id: String; let name: String }
        let configured: Bool
        let url: String
        let user: String?
        let message: String?
        let libraries: [Library]
    }
    let audiobookshelf: Audiobookshelf
}

private let bookKinds: [(String, String)] = [("book", "Books"), ("author", "Authors"), ("series", "Series")]

/// Books › Library
struct BooksView: View {
    let model: PanelModel
    let overview: Overview
    @AppStorage("musicSpeakers") private var speakersJSON = "[]"
    @State private var cards: CardsState?
    @State private var state: BooksState?
    @State private var zonesVersion = 0

    private var selected: [String] {
        (try? JSONDecoder().decode([String].self, from: Data(speakersJSON.utf8))) ?? []
    }

    private func choose(_ ids: [String]) {
        speakersJSON = String(data: (try? JSONEncoder().encode(ids)) ?? Data("[]".utf8), encoding: .utf8) ?? "[]"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let scan = cards?.scan {
                CardScanView(model: model, scan: scan, canManage: overview.canManage) { cards = $0 }
            }
            ZonesView(model: model, canManage: overview.canManage, version: zonesVersion)
            SpeakersView(model: model, selected: selected, canManage: overview.canManage, choose: choose)
            if let state, !state.audiobookshelf.configured {
                PanelSection("Library") {
                    Text("Connect Audiobookshelf under Books › Sources to see your audiobooks here.").foregroundStyle(.secondary)
                    Button("Books › Sources") { model.selection = .bookSources }.buttonStyle(.link)
                }
            } else {
                BookLibraryView(model: model, selected: selected, canManage: overview.canManage, unknown: cards?.unknown?.uid,
                                played: { zonesVersion += 1 }, cardsChanged: { cards = $0 })
            }
        }
        .task { state = try? await model.api.get("books") }
        .task {
            while !Task.isCancelled {
                cards = (try? await model.api.get("cards")) ?? cards
                try? await Task.sleep(for: .seconds(cards?.scan != nil ? 1 : 4))
            }
        }
    }
}

private struct BookLibraryView: View {
    let model: PanelModel
    let selected: [String]
    let canManage: Bool
    let unknown: String?
    let played: () -> Void
    let cardsChanged: (CardsState) -> Void
    @State private var kind = "book"
    @State private var query = ""
    @State private var limit = 60
    @State private var items: [LibItem] = []
    @State private var loading = false
    @State private var error: String?
    @State private var path: [String] = []

    var body: some View {
        PanelSection("Library") {
            if let top = path.last {
                if top.hasPrefix("abs|book|") {
                    BookDetailView(model: model, id: top, selected: selected, canManage: canManage, unknown: unknown,
                                   back: { path.removeLast() }, played: played, cardsChanged: cardsChanged)
                        .id(top)
                } else {
                    ShelfView(model: model, id: top, open: { path.append($0) }, back: { path.removeLast() }).id(top)
                }
            } else {
                HStack {
                    Picker("Show", selection: $kind) {
                        ForEach(bookKinds, id: \.0) { Text($0.1).tag($0.0) }
                    }
                    .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 280)
                    TextField("Search", text: $query).textFieldStyle(.roundedBorder)
                }
                if let error { ErrorText(error) }
                if loading && items.isEmpty { ProgressView().controlSize(.small) }
                if !loading && items.isEmpty && error == nil { Text("Nothing found.").foregroundStyle(.secondary) }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 130), alignment: .top)], alignment: .leading, spacing: 14) {
                    ForEach(items) { item in Tile(model: model, item: item) { path.append(item.id) } }
                }
                if query.isEmpty && items.count >= limit { Button("More") { limit += 60 } }
            }
        }
        .task(id: "\(kind)|\(query)|\(limit)") {
            try? await Task.sleep(for: .milliseconds(350))
            if Task.isCancelled { return }
            loading = true
            defer { loading = false }
            do {
                items = try await model.api.get("music/browse", ["source": "abs", "kind": kind,
                                                                 "q": query.trimmingCharacters(in: .whitespaces),
                                                                 "limit": "\(limit)"], as: Items.self).items
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
        }
        .onChange(of: kind) { limit = 60 }
    }
}

/// An author's or a series' books.
private struct ShelfView: View {
    let model: PanelModel
    let id: String
    let open: (String) -> Void
    let back: () -> Void
    @State private var opened: Opened?
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button("‹ Back", action: back).buttonStyle(.link)
            if let error { ErrorText(error) }
            if let opened {
                HStack(alignment: .bottom, spacing: 18) {
                    MusicArtwork(url: model.musicArt(opened.item.image), size: 120, round: opened.item.kind == "author",
                                 symbol: "book.closed")
                    VStack(alignment: .leading, spacing: 4) {
                        Text(opened.item.kind).font(.caption).foregroundStyle(.secondary)
                        Text(opened.item.name).font(.title2.weight(.semibold))
                        if !opened.item.artist.isEmpty { Text(opened.item.artist).foregroundStyle(.secondary) }
                    }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 130), alignment: .top)], alignment: .leading, spacing: 14) {
                    ForEach(opened.albums ?? []) { book in Tile(model: model, item: book) { open(book.id) } }
                }
            } else if error == nil {
                ProgressView().controlSize(.small)
            }
        }
        .task {
            do { opened = try await model.api.get("music/item", ["id": id]) } catch { self.error = error.localizedDescription }
        }
    }
}

/// One audiobook: where you are in it, its chapters, and playing it.
private struct BookDetailView: View {
    let model: PanelModel
    let id: String
    let selected: [String]
    let canManage: Bool
    let unknown: String?
    let back: () -> Void
    let played: () -> Void
    let cardsChanged: (CardsState) -> Void
    @State private var opened: Opened?
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button("‹ Back", action: back).buttonStyle(.link)
            if let error { ErrorText(error) }
            if let opened {
                let book = opened.item
                let progress = opened.progress
                let started = progress.map { !$0.finished && $0.position > 5 } ?? false
                HStack(alignment: .top, spacing: 18) {
                    MusicArtwork(url: model.musicArt(book.image), size: 150, symbol: "book.closed")
                    VStack(alignment: .leading, spacing: 6) {
                        Text("audiobook").font(.caption).foregroundStyle(.secondary)
                        Text(book.name).font(.title2.weight(.semibold))
                        if let subtitle = book.subtitle { Text(subtitle).foregroundStyle(.secondary) }
                        Text([book.artist, book.narrator.map { "read by \($0)" } ?? "", book.album, longClock(book.duration)]
                            .filter { !$0.isEmpty }.joined(separator: " · ")).foregroundStyle(.secondary)
                        if let progress {
                            HStack {
                                ProgressView(value: min(progress.position, book.duration ?? progress.position),
                                             total: max(book.duration ?? 1, 1))
                                    .frame(maxWidth: 300)
                                Text(progress.finished ? "Finished" : "\(longClock(progress.position)) of \(longClock(book.duration))")
                                    .font(.callout).foregroundStyle(.secondary)
                            }
                        }
                        if canManage {
                            HStack {
                                Button(started ? "Resume at \(longClock(progress?.position))" : "Play") { play(nil) }
                                    .buttonStyle(.borderedProminent)
                                if started { Button("From the Start") { play(0) } }
                                if let unknown {
                                    Button("Link Card \(unknown)") { link(book, uid: unknown) }.disabled(selected.count != 1)
                                }
                                Button("Link Next Card") { link(book, uid: nil) }
                                    .disabled(selected.count != 1)
                                    .help(selected.count == 1 ? "Then hold a blank card on a Movie Remote or a reader in Home Assistant"
                                          : "Choose one speaker for the card")
                            }
                            .disabled(selected.isEmpty || busy)
                        }
                        if let description = book.description {
                            Text(description).font(.callout).foregroundStyle(.secondary).lineLimit(4).frame(maxWidth: 520, alignment: .leading)
                        }
                    }
                }
                if let chapters = opened.chapters, !chapters.isEmpty {
                    VStack(spacing: 0) {
                        ForEach(Array(chapters.enumerated()), id: \.offset) { index, chapter in
                            let here = progress.map { !$0.finished && $0.position >= chapter.start && $0.position < chapter.end } ?? false
                            Divider()
                            HStack {
                                Text("\(index + 1)").foregroundStyle(.secondary).frame(width: 28, alignment: .trailing)
                                Text(chapter.title).fontWeight(here ? .semibold : .regular)
                                if here { Text("you're here").font(.caption).foregroundStyle(.secondary) }
                                Spacer()
                                Text(longClock(chapter.end - chapter.start)).foregroundStyle(.secondary)
                                if canManage {
                                    Button { play(chapter.start) } label: { Image(systemName: "play.fill") }
                                        .buttonStyle(.borderless).help("Play from this chapter").disabled(selected.isEmpty)
                                }
                            }
                            .padding(.vertical, 5)
                            .background(here ? Color.accentColor.opacity(0.1) : .clear)
                        }
                    }
                }
            } else if error == nil {
                ProgressView().controlSize(.small)
            }
        }
        .task { await load() }
    }

    private func load() async {
        do { opened = try await model.api.get("music/item", ["id": id]) } catch { self.error = error.localizedDescription }
    }

    private func play(_ position: Double?) {
        busy = true
        var body: [String: Any] = ["ids": [id], "speakers": selected, "option": "replace"]
        if let position { body["position"] = position }
        Task {
            if await musicAction(model, "music/play", body, done: "Playing") { played() }
            busy = false
        }
    }

    private func link(_ book: LibItem, uid: String?) {
        guard let speaker = selected.first else { return }
        busy = true
        Task {
            do {
                let state: CardsState = uid != nil
                    ? try await model.api.post("cards/link", ["uid": uid!, "music": book.cardBody, "speaker": speaker])
                    : try await model.api.post("cards/scan", ["movies": [["music": book.cardBody, "speaker": speaker]]])
                cardsChanged(state)
                model.say(uid.map { "Card \($0) linked" } ?? "Hold a blank card on a reader")
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            busy = false
        }
    }
}

/// Books › Sources: the Audiobookshelf connection.
struct BooksSourcesView: View {
    let model: PanelModel
    let canManage: Bool
    @State private var state: BooksState?
    @State private var url = ""
    @State private var token = ""
    @State private var busy = false

    var body: some View {
        PanelSection("Audiobookshelf") {
            HStack {
                if let abs = state?.audiobookshelf {
                    if !abs.configured {
                        Pill(text: "Not connected", tone: .plain)
                    } else if abs.message != nil {
                        Pill(text: "Can't reach it", tone: .bad)
                    } else {
                        Pill(text: "Connected" + (abs.user.map { " as \($0)" } ?? ""), tone: .good)
                    }
                    ForEach(abs.libraries) { Pill(text: $0.name, tone: .plain) }
                }
            }
            if let message = state?.audiobookshelf.message { Text(message).foregroundStyle(.red) }
            if canManage {
                Text("Audiobooks come from your Audiobookshelf server. Give Marquee its address (such as http://nas.local:13378) "
                     + "and an API key: in Audiobookshelf, open **Settings › API Keys** and add one called Marquee. The key is "
                     + "kept on this Mac only. Books play on the same speakers as music, pick up where you left off, and "
                     + "Audiobookshelf hears how far you got.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                TextField("http://nas.local:13378", text: $url).textFieldStyle(.roundedBorder).frame(maxWidth: 420)
                HStack {
                    SecureField(state?.audiobookshelf.configured == true ? "Keep the key there is" : "API key", text: $token)
                        .textFieldStyle(.roundedBorder).frame(maxWidth: 320)
                    Button(busy ? "Checking…" : "Connect") { save(url: url, token: token) }
                        .buttonStyle(.borderedProminent)
                        .disabled(busy || url.isEmpty || (token.isEmpty && state?.audiobookshelf.configured != true))
                    if state?.audiobookshelf.configured == true {
                        Button("Disconnect", role: .destructive) {
                            guard confirm("Disconnect Audiobookshelf?", detail: "Book cards stop working until it's connected again.",
                                          button: "Disconnect") else { return }
                            save(url: "", token: "")
                        }
                    }
                }
            } else {
                Text("Open the control panel on this Mac's server to change this.").foregroundStyle(.secondary)
            }
        }
        .task {
            state = try? await model.api.get("books")
            if url.isEmpty { url = state?.audiobookshelf.url ?? "" }
        }
    }

    private func save(url: String, token: String) {
        busy = true
        var body: [String: Any] = ["url": url]
        if !token.isEmpty { body["token"] = token }
        Task {
            do {
                state = try await model.api.post("books/audiobookshelf", body)
                self.token = ""
                model.say(url.isEmpty ? "Audiobookshelf disconnected" : "Connected to Audiobookshelf")
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            busy = false
        }
    }
}
