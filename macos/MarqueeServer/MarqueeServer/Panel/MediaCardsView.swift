import SwiftUI

/// Choose albums (or playlists) or audiobooks and the speaker they play on, then scan a card
/// for each in turn. The movie scan's counterpart for Music and Books.
struct MediaScanSetupView: View {
    let model: PanelModel
    let books: Bool
    let canManage: Bool
    let cards: [Card]
    let update: (CardsState) -> Void
    @AppStorage("musicSpeakers") private var speakersJSON = "[]"
    @State private var sources: [MusicSourcesState.Source] = []
    @State private var speakers: [MusicSpeaker] = []
    @State private var source = ""
    @State private var mode = ""
    @State private var parents: [LibItem] = []
    @State private var parent = ""
    @State private var items: [LibItem] = []
    @State private var picked: Set<String> = []
    @State private var search = ""
    @State private var speaker = ""
    @State private var skipLinked = true
    @State private var loading = false
    @State private var error: String?
    @State private var busy = false

    private var modes: [(String, String)] {
        books ? [("book", "All Books"), ("author", "An Author's"), ("series", "A Series")]
              : [("album", "Albums"), ("playlist", "Playlists"), ("artist", "An Artist's Albums")]
    }
    private var from: String { books ? "abs" : source }
    private var needsParent: Bool { ["artist", "author", "series"].contains(mode) }
    private var hasCard: Set<String> { Set(cards.compactMap { $0.music?.id }) }
    private var queue: [LibItem] { items.filter { picked.contains($0.id) && !(skipLinked && hasCard.contains($0.id)) } }
    private var shown: [LibItem] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return q.isEmpty ? items : items.filter { "\($0.name) \($0.artist)".lowercased().contains(q) }
    }
    private var thing: String { books ? "book" : "album" }

    var body: some View {
        PanelSection("Scan cards", subtitle: "Link blank NFC cards to \(books ? "audiobooks" : "albums or playlists") without "
                     + "writing anything on them. Choose them and the speaker they play on, start the scan, then hold each "
                     + "\(thing)'s card on a Movie Remote when it comes up here."
                     + (books ? " A book card picks up where you left off." : "")) {
            HStack {
                if !books {
                    Picker("Source", selection: $source) {
                        if sources.isEmpty { Text("No music sources").tag("") }
                        ForEach(sources) { Text($0.name).tag($0.id) }
                    }
                    .labelsHidden().frame(maxWidth: 200)
                }
                Picker("Choose", selection: $mode) {
                    ForEach(modes, id: \.0) { Text($0.1).tag($0.0) }
                }
                .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 380)
                if needsParent {
                    Picker(mode.capitalized, selection: $parent) {
                        Text("Choose \(mode == "artist" ? "an artist" : mode == "author" ? "an author" : "a series")").tag("")
                        ForEach(parents) { Text($0.name).tag($0.id) }
                    }
                    .labelsHidden().frame(maxWidth: 240)
                }
            }
            if let error { ErrorText(error) }
            if loading { ProgressView().controlSize(.small) }
            if !items.isEmpty || (!loading && (!needsParent || !parent.isEmpty)) {
                HStack {
                    TextField("Search \(thing)s", text: $search).textFieldStyle(.roundedBorder).frame(maxWidth: 260)
                    Text("\(picked.count) picked").foregroundStyle(.secondary)
                    Button("Pick All Shown") { picked.formUnion(shown.map(\.id)) }
                    Button("Clear") { picked = [] }.disabled(picked.isEmpty)
                }
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        if items.isEmpty { Text("Nothing here.").foregroundStyle(.secondary) }
                        ForEach(shown) { item in
                            Toggle(isOn: Binding(get: { picked.contains(item.id) },
                                                 set: { if $0 { picked.insert(item.id) } else { picked.remove(item.id) } })) {
                                HStack(spacing: 8) {
                                    MusicArtwork(url: model.musicArt(item.image), size: 30,
                                                 symbol: books ? "book.closed" : "music.note")
                                    Text(item.name).bold()
                                    Text([item.artist, item.year.map(String.init) ?? ""].filter { !$0.isEmpty }
                                        .joined(separator: " · ")).foregroundStyle(.secondary)
                                    if hasCard.contains(item.id) { Pill(text: "Has a card", tone: .good) }
                                }
                            }
                            .toggleStyle(.checkbox)
                        }
                    }
                    .padding(6)
                }
                .frame(maxHeight: 320)
                .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
            }
            HStack {
                Picker("Plays on", selection: $speaker) {
                    Text("Choose the speaker they play on").tag("")
                    ForEach(speakers.filter { $0.syncedTo == nil }) { Text($0.name + ($0.group ? " (group)" : "")).tag($0.id) }
                }
                .frame(maxWidth: 360)
                Toggle("Skip \(thing)s that already have a card", isOn: $skipLinked).toggleStyle(.checkbox)
            }
            HStack {
                Button(queue.isEmpty ? "Scan Cards" : "Scan \(queue.count) Card\(queue.count == 1 ? "" : "s")", action: start)
                    .buttonStyle(.borderedProminent)
                    .disabled(!canManage || queue.isEmpty || speaker.isEmpty || busy)
                    .help(speaker.isEmpty ? "Choose a speaker first" : "")
                let already = items.filter { picked.contains($0.id) && hasCard.contains($0.id) }.count
                if skipLinked && already > 0 { Text("\(already) already have a card").foregroundStyle(.secondary) }
            }
        }
        .task {
            if mode.isEmpty { mode = modes[0].0 }
            if !books {
                sources = ((try? await model.api.get("music/sources", as: MusicSourcesState.self).sources) ?? [])
                    .filter { $0.searchOnly != true }
                if source.isEmpty { source = sources.first?.id ?? "" }
            }
            speakers = (try? await model.api.get("music/speakers", as: Speakers.self).speakers) ?? []
            let chosen = (try? JSONDecoder().decode([String].self, from: Data(speakersJSON.utf8))) ?? []
            if speaker.isEmpty, let first = chosen.first { speaker = first }
        }
        .task(id: "\(from)|\(mode)") {
            parent = ""
            parents = []
            guard !from.isEmpty, needsParent else { return }
            parents = (try? await model.api.get("music/browse", ["source": from, "kind": mode, "limit": "500"], as: Items.self).items) ?? []
        }
        .task(id: "\(from)|\(mode)|\(parent)") { await load() }
    }

    private func load() async {
        items = []
        guard !from.isEmpty, !mode.isEmpty, !needsParent || !parent.isEmpty else { return }
        loading = true
        defer { loading = false }
        do {
            items = needsParent
                ? try await model.api.get("music/item", ["id": parent], as: Opened.self).albums ?? []
                : try await model.api.get("music/browse", ["source": from, "kind": mode, "limit": "1000"], as: Items.self).items
            picked = Set(items.map(\.id))  // everything, until you untick some
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func start() {
        busy = true
        let movies = queue.map { ["music": $0.cardBody, "speaker": speaker] as [String: Any] }
        Task {
            await cardAction(model, "cards/scan", ["movies": movies], update: update)
            busy = false
        }
    }
}
