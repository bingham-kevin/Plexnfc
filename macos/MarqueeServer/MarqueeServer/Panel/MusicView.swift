import AppKit
import SwiftUI

// The Music tab: music from every source (Music Assistant, Plex, Jellyfin, music folders),
// played on AirPlay, Cast and Music Assistant speakers. Same API as the web panel's Music tab.

// MARK: - What the server sends

/// Something in the music library, from any source.
struct LibItem: Decodable, Sendable, Identifiable, Hashable {
    let id: String  // source|kind|ref
    let source: String
    let kind: String
    let name: String
    let artist: String
    let album: String
    let image: String?
    let duration: Double?
    let number: Int?
    let year: Int?
    // Audiobooks
    var narrator: String?
    var subtitle: String?
    var description: String?
    /// How many books an author or a series has
    var count: Int?
    /// A book's file: where it starts in the book
    var offset: Double?

    var isBook: Bool { source == "abs" }

    /// As a music card keeps it
    var cardBody: [String: Any] {
        ["id": id, "name": name, "artist": artist, "image": image ?? NSNull()]
    }
}

struct MusicSourcesState: Decodable, Sendable {
    struct Source: Decodable, Sendable, Identifiable { let id: String; let name: String; let kind: String; let searchOnly: Bool? }
    struct Folder: Decodable, Sendable, Identifiable { let id: String; let name: String; let path: String; let tracks: Int; let scanning: Bool }
    struct Kinds: Decodable, Sendable { let airplay: Bool; let cast: Bool; let musicAssistant: Bool }
    let sources: [Source]
    let folders: [Folder]
    let speakers: Kinds
}

struct MusicSpeaker: Decodable, Sendable, Identifiable {
    let id: String  // ma:…, airplay:… or cast:…
    let name: String
    let kind: String  // music_assistant, airplay or cast
    let group: Bool
    let volume: Double?
    let state: String?
    let members: [String]
    let syncedTo: String?
    let pairing: String?  // AirPlay: "needed" until paired with the PIN it shows, then "paired" ("linked": paired under Apple TVs)
}

struct MusicZone: Decodable, Sendable, Identifiable {
    struct Track: Decodable, Sendable { let name: String?; let artist: String?; let album: String?; let image: String?; let duration: Double? }
    let id: String
    let kind: String
    let speakers: [String]
    let names: [String]
    let state: String
    let restartsOnResume: Bool?
    let track: Track
    let position: Double?
    let error: String?
    /// An audiobook: where it's got to in the whole book
    let book: BookPlace?

    struct BookPlace: Decodable, Sendable { let id: String; let position: Double?; let duration: Double }
}

struct MusicQueue: Decodable, Sendable {
    struct Item: Decodable, Sendable { let name: String?; let artist: String?; let duration: Double? }
    let index: Int
    let items: [Item]
}

struct Items: Decodable, Sendable { let items: [LibItem] }
struct Opened: Decodable, Sendable {
    struct Chapter: Decodable, Sendable { let title: String; let start: Double; let end: Double }
    struct Progress: Decodable, Sendable { let position: Double; let finished: Bool }
    let item: LibItem
    let albums: [LibItem]?
    let tracks: [LibItem]?
    let chapters: [Chapter]?
    let progress: Progress?
}
struct Speakers: Decodable, Sendable { let speakers: [MusicSpeaker] }
private struct Zones: Decodable, Sendable { let zones: [MusicZone] }

extension PanelModel {
    /// A music picture's address: Music Assistant's own, or the server's (relative to it).
    func musicArt(_ image: String?) -> URL? {
        guard let image, !image.isEmpty else { return nil }
        return image.hasPrefix("http") ? URL(string: image) : URL(string: image, relativeTo: api.base)
    }
}

/// POST from the Music tab; says what went wrong. True if it worked.
@MainActor
@discardableResult
func musicAction(_ model: PanelModel, _ path: String, _ body: [String: Any] = [:], done: String? = nil) async -> Bool {
    do {
        try await model.api.post(path, body)
        if let done { model.say(done) }
        return true
    } catch {
        model.say(error.localizedDescription, error: true)
        return false
    }
}

private func clock(_ seconds: Double?) -> String {
    guard let seconds, seconds >= 0 else { return "" }
    return String(format: "%d:%02d", Int(seconds) / 60, Int(seconds) % 60)
}

/// 3:07, or 7:12:05 for something over an hour
func longClock(_ seconds: Double?) -> String {
    guard let seconds, seconds >= 0 else { return "" }
    let s = Int(seconds)
    return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s % 3600 / 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
}

private let kindLabels: [(String, String)] = [("album", "Albums"), ("artist", "Artists"), ("playlist", "Playlists"), ("track", "Tracks")]
private let speakerKinds: [(String, String)] = [("music_assistant", "Music Assistant"), ("airplay", "AirPlay"), ("cast", "Cast")]

// MARK: - The tab

struct MusicView: View {
    let model: PanelModel
    let overview: Overview
    @AppStorage("musicSpeakers") private var speakersJSON = "[]"
    @State private var cards: CardsState?
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
            LibraryView(model: model, selected: selected, canManage: overview.canManage, unknown: cards?.unknown?.uid,
                        played: { zonesVersion += 1 }, cardsChanged: { cards = $0 })
        }
        .task {
            while !Task.isCancelled {
                cards = (try? await model.api.get("cards")) ?? cards
                try? await Task.sleep(for: .seconds(cards?.scan != nil ? 1 : 4))
            }
        }
    }
}

// MARK: - What's playing

struct ZonesView: View {
    let model: PanelModel
    let canManage: Bool
    let version: Int
    @State private var zones: [MusicZone] = []

    var body: some View {
        Group {
            if !zones.isEmpty {
                PanelSection("Playing") {
                    ForEach(zones) { zone in
                        ZoneRow(model: model, zone: zone, canManage: canManage) { zones = $0 }
                        if zone.id != zones.last?.id { Divider() }
                    }
                }
            }
        }
        .task(id: version) {
            while !Task.isCancelled {
                zones = (try? await model.api.get("music/now", as: Zones.self).zones) ?? zones
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }
}

private struct ZoneRow: View {
    let model: PanelModel
    let zone: MusicZone
    let canManage: Bool
    let update: ([MusicZone]) -> Void
    @State private var showQueue = false
    @State private var queue: MusicQueue?

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            MusicArtwork(url: model.musicArt(zone.track.image), size: 88)
            VStack(alignment: .leading, spacing: 4) {
                Text(zone.track.name ?? "—").font(.headline)
                Text([zone.track.artist, zone.track.album].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .foregroundStyle(.secondary)
                Text(zone.names.joined(separator: " + ")
                     + (zone.book.map { " · \(longClock($0.position)) of \(longClock($0.duration)) in the book" }
                        ?? (zone.track.duration != nil ? " · \(clock(zone.position)) / \(clock(zone.track.duration))" : ""))
                     + (zone.kind == "marquee" && zone.speakers.count > 1 ? " · not in sync" : ""))
                    .font(.callout).foregroundStyle(.secondary)
                if let error = zone.error { Text(error).font(.callout).foregroundStyle(.red) }
                if canManage {
                    HStack {
                        control("backward.fill", "previous")
                        control(zone.state == "paused" ? "play.fill" : "pause.fill", zone.state == "paused" ? "play" : "pause")
                            .help(zone.state == "paused" && zone.restartsOnResume == true ? "AirPlay starts the track again" : "")
                        control("forward.fill", "next")
                        control("stop.fill", "stop")
                        Button(showQueue ? "Hide Queue" : "Queue") { showQueue.toggle() }.buttonStyle(.link)
                    }
                }
                if showQueue, let queue {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(queue.items.enumerated()), id: \.offset) { index, item in
                            Button {
                                Task { await send("jump", value: index) }
                            } label: {
                                HStack {
                                    Text("\(index + 1).").foregroundStyle(.secondary).frame(width: 28, alignment: .trailing)
                                    Text(item.name ?? "").fontWeight(index == queue.index ? .bold : .regular)
                                    if let artist = item.artist, !artist.isEmpty { Text(artist).foregroundStyle(.secondary) }
                                    Spacer()
                                    Text(clock(item.duration)).foregroundStyle(.secondary)
                                }
                            }
                            .buttonStyle(.plain)
                            .disabled(!canManage)
                        }
                    }
                    .font(.callout)
                }
            }
        }
        .task(id: showQueue) {
            while showQueue, !Task.isCancelled {
                queue = try? await model.api.get("music/queue", ["zone": zone.id])
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func control(_ symbol: String, _ action: String) -> some View {
        Button { Task { await send(action) } } label: { Image(systemName: symbol).frame(width: 22) }
    }

    private func send(_ action: String, value: Int? = nil) async {
        var body: [String: Any] = ["zone": zone.id, "action": action]
        if let value { body["value"] = value }
        do {
            let answer: Zones = try await model.api.post("music/control", body)
            update(answer.zones)
        } catch {
            model.say(error.localizedDescription, error: true)
        }
    }
}

// MARK: - Speakers

struct SpeakersView: View {
    let model: PanelModel
    let selected: [String]
    let canManage: Bool
    let choose: ([String]) -> Void
    @State private var speakers: [MusicSpeaker]?

    var body: some View {
        PanelSection(title: "Speakers", trailing: {
            if !selected.isEmpty { Button("Clear") { choose([]) }.buttonStyle(.link) }
        }) {
            if let speakers {
                if speakers.isEmpty {
                    Text("No speakers yet. AirPlay and Cast speakers on the network show up here on their own; for Music "
                         + "Assistant's, connect it under Music Settings below.").foregroundStyle(.secondary)
                }
                ForEach(speakerKinds, id: \.0) { kind, label in
                    let list = speakers.filter { $0.kind == kind && $0.syncedTo == nil }
                    if !list.isEmpty {
                        Text(label).font(.caption).foregroundStyle(.secondary)
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), alignment: .leading)], alignment: .leading, spacing: 8) {
                            ForEach(list) { speaker in
                                SpeakerTile(model: model, speaker: speaker, all: speakers, on: selected.contains(speaker.id),
                                            canManage: canManage, reload: reload) {
                                    choose(selected.contains(speaker.id) ? selected.filter { $0 != speaker.id } : selected + [speaker.id])
                                }
                            }
                        }
                    }
                }
                let chosen = speakers.filter { selected.contains($0.id) }
                if chosen.count > 1 {
                    Text(Set(chosen.map(\.kind)).count > 1
                         ? "These play the same music, but AirPlay, Cast and Music Assistant speakers aren't in sync with each other."
                         : chosen[0].kind == "music_assistant" ? "Music Assistant plays these in sync, as a group."
                         : "Marquee sends these the same tracks; they may drift apart a little. For Cast speakers in sync, use a speaker group from Google Home.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            } else {
                ProgressView().controlSize(.small)
            }
        }
        .task {
            while !Task.isCancelled {
                await reload()
                try? await Task.sleep(for: .seconds(speakers?.isEmpty ?? true ? 3 : 10))
            }
        }
    }

    private func reload() async {
        speakers = (try? await model.api.get("music/speakers", as: Speakers.self).speakers) ?? speakers
    }
}

private struct SpeakerTile: View {
    let model: PanelModel
    let speaker: MusicSpeaker
    let all: [MusicSpeaker]
    let on: Bool
    let canManage: Bool
    let reload: () async -> Void
    let toggle: () -> Void
    @State private var level: Double?
    @State private var pairID: String?
    @State private var pin = ""
    @State private var asking = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle(isOn: Binding(get: { on }, set: { _ in toggle() })) {
                HStack(spacing: 4) {
                    Text(speaker.name + (speaker.group ? " (group)" : ""))
                    if speaker.state == "playing" { Circle().fill(.green).frame(width: 7, height: 7) }
                }
            }
            .toggleStyle(.checkbox)
            let members = speaker.members.map { m in all.first { $0.id == m }?.name ?? m }
            if !members.isEmpty {
                HStack {
                    Text("+ " + members.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                    if canManage {
                        Button("Ungroup") {
                            Task { await musicAction(model, "music/group", ["leader": speaker.id, "members": []], done: "Ungrouped") }
                        }
                        .buttonStyle(.link).font(.caption)
                    }
                }
            }
            if canManage, let volume = level ?? speaker.volume {
                Slider(value: Binding(get: { volume }, set: { level = $0 }), in: 0...100) { editing in
                    if !editing, let level {
                        Task { await musicAction(model, "music/volume", ["speaker": speaker.id, "level": Int(level)]) }
                    }
                }
                .controlSize(.small)
            }
            if canManage, let pairing = speaker.pairing, pairing != "linked" { pairingControls(pairing) }
        }
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(on ? Color.accentColor : .clear))
    }
}

// Apple TVs ask for pairing before Marquee can stream to them: they show a PIN, typed in here.
private extension SpeakerTile {
    struct Started: Decodable { let pairId: String }

    @ViewBuilder func pairingControls(_ pairing: String) -> some View {
        if pairing == "paired" {
            Button("Unpair") {
                Task {
                    if await musicAction(model, "music/airplay/unpair", ["speaker": speaker.id], done: "Unpaired \(speaker.name)") { await reload() }
                }
            }
            .buttonStyle(.link).font(.caption)
        } else if let pairID {
            HStack(spacing: 6) {
                TextField("PIN on the TV", text: $pin).textFieldStyle(.roundedBorder).onSubmit { finish(pairID) }
                Button("Pair") { finish(pairID) }.disabled(pin.trimmingCharacters(in: .whitespaces).count < 4)
                Button("Cancel") { self.pairID = nil; pin = "" }.buttonStyle(.link)
            }
            .controlSize(.small)
        } else {
            Button(asking ? "Asking…" : "Pair") {
                asking = true
                Task {
                    defer { asking = false }
                    do {
                        pairID = try await model.api.post("music/airplay/pair", ["speaker": speaker.id], as: Started.self).pairId
                    } catch {
                        model.say(error.localizedDescription, error: true)
                    }
                }
            }
            .controlSize(.small).disabled(asking)
        }
    }

    func finish(_ id: String) {
        let typed = pin
        pairID = nil
        pin = ""
        Task {
            if await musicAction(model, "music/airplay/pair/finish", ["pair_id": id, "pin": typed], done: "Paired \(speaker.name)") {
                await reload()
            }
        }
    }
}

// MARK: - The library

private struct LibraryView: View {
    let model: PanelModel
    let selected: [String]
    let canManage: Bool
    let unknown: String?
    let played: () -> Void
    let cardsChanged: (CardsState) -> Void
    @State private var sources: [MusicSourcesState.Source]?
    @State private var source = ""
    @State private var kind = "album"
    @State private var query = ""
    @State private var limit = 60
    @State private var items: [LibItem] = []
    @State private var loading = false
    @State private var error: String?
    @State private var path: [String] = []

    private var current: MusicSourcesState.Source? { sources?.first { $0.id == source } }
    private var browsing: Bool { current != nil && current?.searchOnly != true }

    var body: some View {
        PanelSection("Library") {
            if let sources, sources.isEmpty {
                Text("No music yet. Add a music folder or connect Music Assistant under Music › Sources; Plex music libraries "
                     + "and Jellyfin servers added under Movies › Sources show up here too.").foregroundStyle(.secondary)
            } else if let top = path.last {
                ItemDetailView(model: model, id: top, selected: selected, canManage: canManage, unknown: unknown,
                               open: { path.append($0) }, back: { path.removeLast() }, played: played,
                               cardsChanged: cardsChanged)
                    .id(top)
            } else {
                HStack {
                    Picker("Source", selection: $source) {
                        Text("Search everywhere").tag("")
                        ForEach(sources ?? []) { Text($0.name).tag($0.id) }
                    }
                    .labelsHidden().frame(maxWidth: 200)
                    if browsing {
                        Picker("Show", selection: $kind) {
                            ForEach(kindLabels, id: \.0) { Text($0.1).tag($0.0) }
                        }
                        .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 340)
                    }
                    TextField(browsing ? "Filter" : "Search artists, albums, tracks…", text: $query)
                        .textFieldStyle(.roundedBorder)
                }
                if !browsing && query.trimmingCharacters(in: .whitespaces).count < 2 {
                    Text("Type to search every source, or choose one to browse.").foregroundStyle(.secondary)
                }
                if let error { ErrorText(error) }
                if loading && items.isEmpty { ProgressView().controlSize(.small) }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 130), alignment: .top)], alignment: .leading, spacing: 14) {
                    ForEach(items) { item in
                        Tile(model: model, item: item, sourceName: browsing ? nil : sources?.first { $0.id == item.source }?.name) {
                            path.append(item.id)
                        }
                    }
                }
                if browsing && items.count >= limit {
                    Button("More") { limit += 60 }
                }
            }
        }
        .task { sources = (try? await model.api.get("music/sources", as: MusicSourcesState.self).sources) ?? [] }
        .task(id: "\(source)|\(kind)|\(query)|\(limit)") {
            try? await Task.sleep(for: .milliseconds(350))
            if Task.isCancelled { return }
            let q = query.trimmingCharacters(in: .whitespaces)
            guard browsing || q.count > 1 else { items = []; return }
            loading = true
            defer { loading = false }
            do {
                items = browsing
                    ? try await model.api.get("music/browse", ["source": source, "kind": kind, "q": q, "limit": "\(limit)"], as: Items.self).items
                    : try await model.api.get("music/search", ["q": q, "source": source], as: Items.self).items
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
        }
        .onChange(of: source) { limit = 60 }
        .onChange(of: kind) { limit = 60 }
    }
}

struct Tile: View {
    let model: PanelModel
    let item: LibItem
    var sourceName: String?
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 3) {
                MusicArtwork(url: model.musicArt(item.image), size: 130, round: item.kind == "artist" || item.kind == "author",
                             symbol: item.isBook ? "book.closed" : "music.note")
                Text(item.name).fontWeight(.semibold).lineLimit(2)
                Text([item.kind == "album" || item.kind == "book" ? nil : item.kind, item.artist.isEmpty ? nil : item.artist,
                      item.count.map { "\($0) book\($0 == 1 ? "" : "s")" }, item.year.map(String.init), sourceName]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            .frame(width: 130, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

private struct ItemDetailView: View {
    let model: PanelModel
    let id: String
    let selected: [String]
    let canManage: Bool
    let unknown: String?
    let open: (String) -> Void
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
                let item = opened.item
                HStack(alignment: .bottom, spacing: 18) {
                    MusicArtwork(url: model.musicArt(item.image), size: 150, round: item.kind == "artist")
                    VStack(alignment: .leading, spacing: 6) {
                        Text(item.kind).font(.caption).foregroundStyle(.secondary)
                        Text(item.name).font(.title2.weight(.semibold))
                        Text([item.artist.isEmpty ? nil : item.artist, item.year.map(String.init)].compactMap { $0 }
                            .joined(separator: " · ")).foregroundStyle(.secondary)
                        if canManage {
                            HStack {
                                Group {
                                    Button("Play") { play([item.id], "replace") }.buttonStyle(.borderedProminent)
                                    Button("Play Next") { play([item.id], "next") }
                                    Button("Add to Queue") { play([item.id], "add") }
                                }
                                .disabled(selected.isEmpty)
                                Group {
                                    if let unknown {
                                        Button("Link Card \(unknown)") { link(item, uid: unknown) }
                                    }
                                    Button("Link Next Card") { link(item, uid: nil) }
                                        .help("Then hold a blank card on a reader. It plays on that reader's speakers.")
                                }
                                .disabled(busy)
                            }
                            .disabled(busy)
                        }
                    }
                }
                if let albums = opened.albums {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 130), alignment: .top)], alignment: .leading, spacing: 14) {
                        ForEach(albums) { album in Tile(model: model, item: album) { open(album.id) } }
                    }
                }
                if let tracks = opened.tracks, item.kind != "track" {
                    VStack(spacing: 0) {
                        ForEach(Array(tracks.enumerated()), id: \.offset) { index, track in
                            Divider()
                            HStack {
                                Text("\(track.number ?? index + 1)").foregroundStyle(.secondary).frame(width: 28, alignment: .trailing)
                                Text(track.name)
                                if !track.artist.isEmpty && track.artist != item.artist {
                                    Text(track.artist).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text(clock(track.duration)).foregroundStyle(.secondary)
                                if canManage {
                                    Button { play([id], "replace", start: index) } label: { Image(systemName: "play.fill") }
                                        .buttonStyle(.borderless).help("Play from here")
                                    Button { play([track.id], "add") } label: { Image(systemName: "plus") }
                                        .buttonStyle(.borderless).help("Add to the queue")
                                }
                            }
                            .padding(.vertical, 5)
                            .disabled(selected.isEmpty)
                        }
                    }
                }
            } else if error == nil {
                ProgressView().controlSize(.small)
            }
        }
        .task {
            do {
                opened = try await model.api.get("music/item", ["id": id])
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    private func play(_ ids: [String], _ option: String, start: Int = 0) {
        busy = true
        Task {
            let done = option == "replace" ? "Playing" : option == "next" ? "Playing next" : "Added to the queue"
            if await musicAction(model, "music/play", ["ids": ids, "speakers": selected, "option": option, "start": start],
                                 done: done) {
                played()
            }
            busy = false
        }
    }

    private func link(_ item: LibItem, uid: String?) {
        busy = true
        Task {
            do {
                let state: CardsState = uid != nil
                    ? try await model.api.post("cards/link", ["uid": uid!, "music": item.cardBody])
                    : try await model.api.post("cards/scan", ["movies": [["music": item.cardBody]]])
                cardsChanged(state)
                model.libraryVersion += 1
                model.say(uid.map { "Card \($0) linked" } ?? "Hold a blank card on a reader")
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            busy = false
        }
    }
}

struct CardScanView: View {
    let model: PanelModel
    let scan: Scan
    let canManage: Bool
    let update: (CardsState) -> Void

    var body: some View {
        PanelSection(title: "Linking a card", attention: true, trailing: {
            if canManage {
                Button("Stop") {
                    Task {
                        if let state: CardsState = try? await model.api.post("cards/scan/stop") { update(state) }
                    }
                }
            }
        }) {
            Text("Hold a blank card on a Movie Remote or an NFC reader in Home Assistant for **\(scan.current?.title ?? "the next item")**"
                 + ".")
            if let last = scan.last { Text("Card \(last.uid) plays \(last.title).").foregroundStyle(.secondary) }
        }
    }
}

// MARK: - Settings

/// Music › Sources: music folders, Music Assistant, and the kinds of speaker.
struct MusicSettingsView: View {
    let model: PanelModel
    var canManage = true
    @State private var state: MusicSourcesState?
    @State private var status: MusicStatus?
    @State private var connecting = false

    var body: some View {
        PanelSection("Music sources") {
            Text("Music folders").font(.headline)
            Text("Folders of music files on this Mac or a mounted share. Albums come from the files' tags, or from "
                 + "Artist/Album folders; .m3u files become playlists.").foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(state?.folders ?? []) { folder in
                HStack {
                    Text(folder.name).fontWeight(.semibold)
                    Text(folder.path).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Text(folder.scanning ? "Reading…" : "\(folder.tracks) tracks").foregroundStyle(.secondary)
                    Button("Read Again") { Task { await folderAction("music/folders/\(folder.id)/rescan", done: "Reading the folder again") } }
                        .buttonStyle(.link)
                    Button("Remove", role: .destructive) {
                        guard confirm("Remove \(folder.name)?", detail: "Its files stay where they are.", button: "Remove") else { return }
                        Task { await folderAction("music/folders/\(folder.id)/remove", done: "Folder removed") }
                    }
                    .buttonStyle(.link)
                }
            }
            Button("Add a Folder…", action: addFolder)

            Divider()
            Text("Music Assistant").font(.headline)
            HStack {
                if let status {
                    if status.assistant.connected {
                        Pill(text: "Connected directly", tone: .good)
                    } else if !status.assistant.url.isEmpty {
                        Pill(text: "Can't reach it", tone: .bad)
                    } else {
                        Pill(text: "Not connected directly", tone: .plain)
                    }
                    if status.homeAssistant {
                        Pill(text: status.via == "home_assistant" ? "Home Assistant (in use)" : "Home Assistant",
                             tone: status.via == "home_assistant" ? .good : .plain)
                    }
                    Button(status.assistant.url.isEmpty ? "Connect" : "Change") { connecting = true }.buttonStyle(.link)
                }
            }
            if let message = status?.assistant.message, status?.assistant.connected == false {
                Text(message).foregroundStyle(.red)
            }

            Divider()
            Text("AirPlay and Cast").font(.headline)
            Text("AirPlay: \(state?.speakers.airplay == true ? "on" : "needs pyatv") · Cast: \(state?.speakers.cast == true ? "on" : "needs pychromecast"). "
                 + "HomePods and Apple TVs need **Allow speaker access** set to everyone on the same network (in the Home "
                 + "app). AirPlay can't pause a stream, so Play after Pause starts the track again: where it was, if this "
                 + "Mac has ffmpeg. File types an AirPlay or Cast speaker can't play go through ffmpeg too.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .disabled(!canManage)
        .task {
            while !Task.isCancelled {
                state = (try? await model.api.get("music/sources")) ?? state
                status = (try? await model.api.get("music")) ?? status
                try? await Task.sleep(for: .seconds(state?.folders.contains { $0.scanning } == true ? 2 : 15))
            }
        }
        .sheet(isPresented: $connecting) {
            if let status { MusicAssistantSheet(model: model, status: status) { self.status = $0 } }
        }
    }

    private func addFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use This Folder"
        panel.message = "Choose the folder that holds your music"
        panel.directoryURL = URL(fileURLWithPath: NSHomeDirectory() + "/Music")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await folderAction("music/folders", ["path": url.path], done: "Folder added") }
    }

    private func folderAction(_ path: String, _ body: [String: Any] = [:], done: String) async {
        do {
            state = try await model.api.post(path, body)
            model.say(done)
        } catch {
            model.say(error.localizedDescription, error: true)
        }
    }
}

/// Music Assistant's address and a token, so Marquee reaches it without Home Assistant.
struct MusicAssistantSheet: View {
    let model: PanelModel
    let status: MusicStatus
    let update: (MusicStatus) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var url = ""
    @State private var token = ""
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Music Assistant").font(.title2.weight(.semibold))
            Text("Marquee can talk to Music Assistant itself, for its library and speakers, even when Home Assistant is "
                 + "down. Give it Music Assistant's address (port 8095, such as http://homeassistant.local:8095) and a "
                 + "long-lived token: in Music Assistant, open **Settings › Profile**, and under **Long-lived access "
                 + "tokens** make one called Marquee. The token is kept on this Mac only.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextField("Address", text: $url, prompt: Text("http://homeassistant.local:8095"))
                .textFieldStyle(.roundedBorder)
            HStack {
                SecureField("Token", text: $token,
                            prompt: Text(status.assistant.token ? "Keep the token there is" : "Long-lived token"))
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(save)
                Button(busy ? "Checking…" : "Connect", action: save)
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || url.trimmingCharacters(in: .whitespaces).isEmpty
                              || (token.isEmpty && !status.assistant.token))
            }
            HStack {
                if !status.assistant.url.isEmpty {
                    Button("Disconnect", role: .destructive) {
                        guard confirm("Stop talking to Music Assistant directly?",
                                      detail: "Music cards then go through Home Assistant, if it's connected.",
                                      button: "Disconnect") else { return }
                        send(["url": ""], done: "Music Assistant disconnected")
                    }
                }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 500)
        .onAppear { url = status.assistant.url }
    }

    private func save() {
        let address = url.trimmingCharacters(in: .whitespaces)
        guard !address.isEmpty else { return }
        var body: [String: Any] = ["url": address]
        if !token.isEmpty { body["token"] = token.trimmingCharacters(in: .whitespacesAndNewlines) }
        send(body, done: "Connected to Music Assistant")
    }

    private func send(_ body: [String: Any], done: String) {
        busy = true
        Task {
            do {
                let state: MusicStatus = try await model.api.post("music/assistant", body)
                update(state)
                model.say(done)
                dismiss()
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            busy = false
        }
    }
}

/// Artwork for music, or a note if there isn't any.
struct MusicArtwork: View {
    let url: URL?
    let size: CGFloat
    var round = false
    var symbol = "music.note"

    var body: some View {
        AsyncImage(url: url) { phase in
            if let image = phase.image {
                image.resizable().aspectRatio(contentMode: .fill)
            } else {
                Rectangle().fill(.quaternary).overlay { Image(systemName: symbol).foregroundStyle(.secondary) }
            }
        }
        .frame(width: size, height: size)
        .clipShape(round ? AnyShape(Circle()) : AnyShape(RoundedRectangle(cornerRadius: size > 60 ? 8 : 4)))
    }
}
