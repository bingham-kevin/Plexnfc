import SwiftUI

/// Blank NFC cards linked to movies by their ID: nothing is written on the cards. Choose the
/// movies, start a scan, and hold each movie's card on a Movie Remote in turn.
struct CardsView: View {
    let model: PanelModel
    let overview: Overview
    @State private var state: CardsState?
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let error { ErrorText(error) }
            if let scan = state?.scan {
                ScanProgressView(model: model, scan: scan, canManage: overview.canManage) { state = $0 }
            } else {
                if let unknown = state?.unknown, overview.canManage {
                    UnknownCardView(model: model, uid: unknown.uid) { state = $0 }
                }
                ScanSetupView(model: model, canManage: overview.canManage) { state = $0 }
                MusicCardsNote()
                if let state { LinkedCardsView(model: model, state: state, canManage: overview.canManage) { self.state = $0 } }
            }
        }
        // Checked every second while a scan is going
        .task {
            while !Task.isCancelled {
                do {
                    state = try await model.api.get("cards")
                    error = nil
                } catch {
                    self.error = error.localizedDescription
                }
                try? await Task.sleep(for: .seconds(state?.scan != nil ? 1 : 3))
            }
        }
    }
}

/// POST to a card route, whose answer is the new card state.
@MainActor
private func cardAction(_ model: PanelModel, _ path: String, _ body: [String: Any] = [:], done: String? = nil,
                        update: @escaping (CardsState) -> Void) async {
    do {
        let state: CardsState = try await model.api.post(path, body)
        update(state)
        model.libraryVersion += 1  // the tag list shows which movies have cards
        if let done { model.say(done) }
    } catch {
        model.say(error.localizedDescription, error: true)
    }
}

private enum ChooseBy: String, CaseIterable, Identifiable {
    case collection, label, source, manual
    var id: String { rawValue }
    var label: String {
        switch self {
        case .collection: "Collection"
        case .label: "Label"
        case .source: "Source"
        case .manual: "Pick movies"
        }
    }
}

/// Choose the movies to scan cards for: a collection, a label, a source, or by hand.
private struct ScanSetupView: View {
    let model: PanelModel
    let canManage: Bool
    let update: (CardsState) -> Void
    @State private var by: ChooseBy = .collection
    @State private var collection = ""
    @State private var label = ""
    @State private var source = ""
    @State private var picked: Set<String> = []
    @State private var search = ""
    @State private var skipLinked = true
    @State private var filters: Filters?
    @State private var movies: [Movie] = []
    @State private var loading = false
    @State private var error: String?
    @State private var starting = false

    private var chosen: String {
        switch by {
        case .collection: collection
        case .label: label
        case .source: source
        case .manual: "manual"
        }
    }

    private var queue: [Movie] {
        guard !chosen.isEmpty else { return [] }
        let list = by == .manual ? movies.filter { picked.contains($0.key) } : movies
        return skipLinked ? list.filter { $0.cards.isEmpty } : list
    }

    private var shown: [Movie] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return q.isEmpty ? movies : movies.filter { "\($0.title) \($0.yearText)".lowercased().contains(q) }
    }

    var body: some View {
        PanelSection("Scan cards", subtitle: "Link blank NFC cards to movies without writing anything on them. Choose the "
                     + "movies, start the scan, then hold each movie's card on a Movie Remote when it comes up here. Once a "
                     + "card is read, the next movie comes up.") {
            HStack(spacing: 12) {
                Picker("Choose movies by", selection: $by) {
                    ForEach(ChooseBy.allCases.filter { $0 != .source || !(filters?.sources.isEmpty ?? true) }) {
                        Text($0.label).tag($0)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                switch by {
                case .collection:
                    Picker("Collection", selection: $collection) {
                        Text(filters == nil ? "Loading collections…" : "Choose a collection").tag("")
                        ForEach(filters?.collections ?? [], id: \.title) { c in
                            Text(c.count.map { "\(c.title) (\($0))" } ?? c.title).tag(c.title)
                        }
                    }
                    .labelsHidden().frame(maxWidth: 260)
                case .label:
                    Picker("Label", selection: $label) {
                        Text(filters == nil ? "Loading labels…" : "Choose a label").tag("")
                        ForEach(filters?.labels ?? [], id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden().frame(maxWidth: 260)
                case .source:
                    Picker("Source", selection: $source) {
                        Text("Choose a source").tag("")
                        ForEach(filters?.sources ?? [], id: \.id) { Text($0.name).tag($0.id) }
                    }
                    .labelsHidden().frame(maxWidth: 260)
                case .manual:
                    EmptyView()
                }
                Toggle("Skip movies that already have a card", isOn: $skipLinked)
            }
            if (by == .collection && filters?.collections.isEmpty == true) || (by == .label && filters?.labels.isEmpty == true) {
                Text("Your library has no \(by == .label ? "labels" : "collections").").foregroundStyle(.secondary)
            }
            if by == .manual { pickList }
            if let error, !chosen.isEmpty { ErrorText(error) }
            if by != .manual, !chosen.isEmpty, !loading, movies.isEmpty, error == nil {
                ErrorText("There are no movies in this \(by.label.lowercased()). Use Pick movies instead.")
            }
            HStack {
                Button(queue.isEmpty ? "Scan Cards" : "Scan \(queue.count) Card\(queue.count == 1 ? "" : "s")") {
                    starting = true
                    Task {
                        let list = queue.map { ["key": $0.key, "title": $0.title, "year": $0.year ?? NSNull()] as [String: Any] }
                        await cardAction(model, "cards/scan", ["movies": list], update: update)
                        starting = false
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canManage || queue.isEmpty || starting)
                if by != .manual, !chosen.isEmpty, !movies.isEmpty {
                    let linked = movies.count - queue.count
                    Text("\(movies.count) movie\(movies.count == 1 ? "" : "s")\(skipLinked && linked > 0 ? ", \(linked) already have a card" : "")")
                        .foregroundStyle(.secondary)
                }
                if loading { ProgressView().controlSize(.small) }
            }
        }
        .task(id: model.libraryVersion) { filters = try? await model.api.get("filters") }
        .task(id: "\(by.rawValue)|\(chosen)|\(model.libraryVersion)") { await load() }
    }

    private var pickList: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                TextField("Search titles", text: $search).textFieldStyle(.roundedBorder).frame(maxWidth: 240)
                Text("\(picked.count) picked").foregroundStyle(.secondary)
                Button("Pick All Shown") { picked.formUnion(shown.map(\.key)) }
                Button("Clear") { picked.removeAll() }.disabled(picked.isEmpty)
            }
            List(shown) { movie in
                Toggle(isOn: Binding(get: { picked.contains(movie.key) },
                                     set: { if $0 { picked.insert(movie.key) } else { picked.remove(movie.key) } })) {
                    HStack {
                        Poster(url: model.poster(movie.key, 80, 120), width: 22)
                        Text(movie.title).bold()
                        Text(movie.yearText).foregroundStyle(.secondary)
                        Spacer()
                        if !movie.cards.isEmpty { Pill(text: "Has a card", tone: .good) }
                    }
                }
            }
            .frame(height: 360)
            .overlay { if loading && movies.isEmpty { ProgressView("Loading the library…") } }
        }
    }

    private func load() async {
        guard !chosen.isEmpty else {
            movies = []
            return
        }
        loading = true
        do {
            let answer: Movies = try await model.api.get("movies", [
                "collection": by == .collection ? collection : "", "label": by == .label ? label : "",
                "source": by == .source ? source : "",
            ])
            movies = answer.movies
            error = nil
        } catch {
            movies = []
            self.error = error.localizedDescription
        }
        loading = false
    }
}

/// The scan in progress: the movie whose card to hold on the reader, and what the last card did.
private struct ScanProgressView: View {
    let model: PanelModel
    let scan: Scan
    let canManage: Bool
    let update: (CardsState) -> Void
    @State private var busy = false

    var body: some View {
        PanelSection(title: "Scanning cards", trailing: {
            Text("\(min(scan.index + 1, scan.total)) of \(scan.total) · \(scan.linked) linked").foregroundStyle(.secondary)
        }) {
            ProgressView(value: Double(scan.index), total: Double(max(scan.total, 1)))
            if let current = scan.current {
                HStack(alignment: .top, spacing: 20) {
                    if let music = current.music {
                        MusicArtwork(url: model.musicArt(music.image), size: 180).id(current.key)
                    } else {
                        Poster(url: model.poster(current.key, 300, 450), width: 180).id(current.key)
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Hold this \(current.music == nil ? "movie" : "music")'s card on a Movie Remote or an NFC reader in Home Assistant")
                            .foregroundStyle(.secondary)
                        Text(current.title).font(.largeTitle.weight(.semibold))
                        if current.music != nil, let speaker = current.speakerName {
                            Text("Plays on \(speaker)").foregroundStyle(.secondary)
                        }
                        if let next = scan.next { Text("Next: \(next.title)").foregroundStyle(.secondary) }
                        lastCard
                        HStack {
                            Button("‹ Back") { step("back") }.disabled(!canManage || scan.index == 0 || busy)
                            Button("Skip ›") { step("skip") }.disabled(!canManage || busy)
                            Button("Stop Scanning", role: .destructive) { step("stop") }.disabled(!canManage || busy)
                        }
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    Text("All done").font(.largeTitle.weight(.semibold))
                    Text("\(scan.linked) card\(scan.linked == 1 ? "" : "s") linked.")
                    if let last = scan.last { Text("Last: \(last.title) (\(last.uid))").foregroundStyle(.secondary) }
                    HStack {
                        Button("‹ Back") { step("back") }.disabled(!canManage || busy)
                        Button("Finish") { step("stop") }.buttonStyle(.borderedProminent).disabled(!canManage || busy)
                    }
                }
            }
            Text("While scanning, cards held on a Movie Remote, or on an NFC reader in Home Assistant, are linked rather than played.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var lastCard: some View {
        if let last = scan.last {
            let fresh = Date().timeIntervalSince1970 - last.at < 6
            Label {
                Text("Linked **\(last.title)** to card \(last.uid)\(last.movedFrom.map { " (it was \($0))" } ?? "")")
            } icon: {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
            .padding(8)
            .background(fresh ? Color.green.opacity(0.15) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
            .animation(.easeOut, value: last.at)
        }
    }

    private func step(_ action: String) {
        busy = true
        Task {
            await cardAction(model, "cards/scan/\(action)", update: update)
            busy = false
        }
    }
}

/// A card was read that isn't linked to anything: pick its movie.
private struct UnknownCardView: View {
    let model: PanelModel
    let uid: String
    let update: (CardsState) -> Void
    @State private var movies: [Movie] = []
    @State private var search = ""
    @State private var linkMusic = false

    private var matches: [Movie] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return q.isEmpty ? [] : Array(movies.filter { $0.title.lowercased().contains(q) }.prefix(8))
    }

    var body: some View {
        PanelSection("Card not linked", subtitle: "A reader just read card \(uid), which isn't linked to anything. "
                     + "Find what it should play:", attention: true) {
            do {
                Picker("Link it to", selection: $linkMusic) {
                    Text("A movie").tag(false)
                    Text("Music").tag(true)
                }
                .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 220)
            }
            if linkMusic {
                Text("Find the music on the **Music** tab, choose one speaker, and use **Link Card \(uid)**.")
                    .foregroundStyle(.secondary)
            } else {
            TextField("Search titles", text: $search).textFieldStyle(.roundedBorder).frame(maxWidth: 300)
            ForEach(matches) { movie in
                Button {
                    Task { await cardAction(model, "cards/link", ["uid": uid, "key": movie.key], done: "Linked to \(movie.title)",
                                            update: update) }
                } label: {
                    HStack {
                        Poster(url: model.poster(movie.key, 80, 120), width: 24)
                        Text(movie.title)
                        Text(movie.yearText).foregroundStyle(.secondary)
                    }
                }
                .buttonStyle(.plain)
            }
            }
        }
        .task { movies = (try? await model.api.get("movies", as: Movies.self).movies) ?? [] }
    }
}

private struct LinkedCardsView: View {
    let model: PanelModel
    let state: CardsState
    let canManage: Bool
    let update: (CardsState) -> Void
    @State private var search = ""

    var body: some View {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        let shown = state.cards.filter { q.isEmpty || "\($0.title) \($0.uid)".lowercased().contains(q) }
        PanelSection("Linked cards") {
            if state.cards.isEmpty {
                Text("No cards linked yet.").foregroundStyle(.secondary)
            } else {
                HStack {
                    TextField("Search titles or card IDs", text: $search).textFieldStyle(.roundedBorder).frame(maxWidth: 280)
                    Text("\(state.cards.count) card\(state.cards.count == 1 ? "" : "s")").foregroundStyle(.secondary)
                }
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(shown) { card in
                        HStack(spacing: 12) {
                            if let music = card.music {
                                MusicArtwork(url: model.musicArt(music.image), size: 39)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(card.title).bold()
                                    Text("♪ \(music.mediaType) on \(card.speakerName ?? "")")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            } else {
                                Poster(url: model.poster(card.key, 80, 120), width: 26)
                                Text(card.title).bold()
                            }
                            Spacer()
                            CodeText(card.uid)
                            if canManage {
                                Button("Unlink") {
                                    guard confirm("Unlink this card from \(card.title)?", button: "Unlink") else { return }
                                    Task {
                                        let uid = card.uid.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? card.uid
                                        await cardAction(model, "cards/\(uid)/remove", update: update)
                                    }
                                }
                            }
                        }
                        Divider()
                    }
                }
            }
        }
    }
}

/// Music cards are linked from the Music tab, where all the music is.
private struct MusicCardsNote: View {
    var body: some View {
        PanelSection("Music cards") {
            Text("A card can play an album, playlist, artist or track instead, from any music source, on any speaker. "
                 + "Find the music on the **Music** tab, choose a speaker, and use **Link Next Card**.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
}
