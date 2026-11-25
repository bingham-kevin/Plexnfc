import SwiftUI

// Devices › Readers: each NFC reader paired to a default output, the speakers its music and book
// cards play on. Same API as the web panel's Readers section.

struct ReaderInfo: Decodable, Sendable, Identifiable {
    struct Now: Decodable, Sendable { let title: String; let state: String }
    let id: String
    let kind: String
    let name: String
    let `default`: [String]
    let speakers: [String]
    let speakerNames: [String]
    let isDefault: Bool
    let moviePlayer: String?
    let page: Int?
    let screen: String?
    let wake: Bool?
    let now: Now?
}

struct ReadersState: Decodable, Sendable {
    struct Speaker: Decodable, Sendable, Identifiable { let id: String; let name: String; let group: Bool }
    let readers: [ReaderInfo]
    let speakers: [Speaker]
    let players: [Player]
    let screens: [String]?
}

struct ReadersView: View {
    let model: PanelModel
    let overview: Overview
    @State private var state: ReadersState?
    @State private var error: String?

    var body: some View {
        PanelSection("Readers", subtitle: "Each NFC reader plays music and book cards on its own speakers, so a card plays "
                     + "in the room where it's held. Choose its default output "
                     + "here. On the reader (a Movie Remote's speaker screen, or the controls Home Assistant gives a "
                     + "Tessera screen) the speakers can be changed, and are remembered until Restore Default, or Make "
                     + "Default keeps them.") {
            if let error { ErrorText(error) }
            if let state {
                if state.readers.isEmpty {
                    Text("No readers yet. Pair a Movie Remote or Home Assistant (with the Marquee integration) above. "
                         + "Each reader in Home Assistant also shows up here once it reads a card.").foregroundStyle(.secondary)
                }
                ForEach(state.readers) { reader in
                    ReaderRow(model: model, reader: reader, state: state, canManage: overview.canManage) { self.state = $0 }
                }
            } else if error == nil {
                ProgressView().controlSize(.small)
            }
        }
        .task {
            while !Task.isCancelled {
                do { state = try await model.api.get("readers"); error = nil } catch { self.error = error.localizedDescription }
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }
}

private struct ReaderRow: View {
    let model: PanelModel
    let reader: ReaderInfo
    let state: ReadersState
    let canManage: Bool
    let update: (ReadersState) -> Void
    @State private var page = ""
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(reader.name).bold()
                Pill(text: ["remote": "Movie Remote", "assistant": "Home Assistant"][reader.kind] ?? "Home Assistant reader", tone: .plain)
                Spacer()
                if canManage && reader.kind == "homeassistant" {
                    Button("Forget", role: .destructive) {
                        guard confirm("Forget \(reader.name)?", detail: "It shows up again when it reads a card.",
                                      button: "Forget") else { return }
                        send("readers/\(path)/remove", [:], done: nil)
                    }
                    .buttonStyle(.link)
                }
            }
            Text(summary).font(.callout).foregroundStyle(.secondary)
            LabeledContent("Music and books play on") {
                Menu {
                    ForEach(state.speakers) { speaker in
                        Toggle(speaker.name + (speaker.group ? " (group)" : ""), isOn: Binding(
                            get: { reader.default.contains(speaker.id) },
                            set: { on in send("readers/\(path)", ["default": next(speaker.id, on)], done: "\(reader.name) saved") }))
                    }
                    ForEach(reader.default.filter { id in !state.speakers.contains { $0.id == id } }, id: \.self) { id in
                        Toggle("\(id) (away)", isOn: Binding(get: { true },
                            set: { _ in send("readers/\(path)", ["default": next(id, false)], done: "\(reader.name) saved") }))
                    }
                    if state.speakers.isEmpty { Text("No speakers found.") }
                } label: {
                    Text(chosenSpeakers).lineLimit(1).truncationMode(.tail)
                }
                .frame(maxWidth: 420, alignment: .leading)
                .help(chosenSpeakers)
            }
            HStack(spacing: 16) {
                Picker("Movies play on", selection: Binding(
                    get: { reader.moviePlayer ?? "" },
                    set: { send("readers/\(path)", ["movie_player": $0.isEmpty ? NSNull() : $0], done: "\(reader.name) saved") })) {
                    Text(reader.kind == "homeassistant" ? "Its Home Assistant's" : "The default player").tag("")
                    ForEach(state.players) { Text($0.name).tag($0.id) }
                }
                .frame(maxWidth: 320)
                Picker("Shows Now Playing on", selection: Binding(
                    get: { reader.screen ?? "" },
                    set: { send("readers/\(path)", ["screen": $0.isEmpty ? NSNull() : $0], done: "\(reader.name) saved") })) {
                    Text(reader.kind == "homeassistant" ? "Its Home Assistant's" : "No screen").tag("")
                    ForEach(screenChoices, id: \.self) { Text($0).tag($0) }
                }
                .frame(maxWidth: 320)
                if reader.screen != nil {
                    Toggle("Turn the TV on for it", isOn: Binding(
                        get: { reader.wake ?? false },
                        set: { send("readers/\(path)", ["wake": $0], done: "\(reader.name) saved") }))
                    .toggleStyle(.checkbox)
                    .help("Else Now Playing shows only when Marquee is already open")
                }
                if reader.kind == "homeassistant" {
                    HStack {
                        Text("Tessera page")
                        TextField("none", text: $page).frame(width: 50).textFieldStyle(.roundedBorder)
                            .onSubmit(savePage)
                        if page != (reader.page.map(String.init) ?? "") { Button("Save", action: savePage) }
                    }
                }
            }
        }
        .disabled(!canManage || busy)
        .padding(10)
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
        .onAppear { page = reader.page.map(String.init) ?? "" }
        .onChange(of: reader.page) { page = reader.page.map(String.init) ?? "" }
    }

    private var screenChoices: [String] {
        let screens = state.screens ?? []
        if let screen = reader.screen, !screens.contains(screen) { return screens + [screen] }
        return screens
    }

    private var path: String { reader.id }  // the API escapes it (appendingPathComponent)

    private var summary: String {
        var text = reader.kind == "assistant"
            ? "Every NFC reader in this Home Assistant (phones with its app too) uses these until it has settings of its own. "
            : ""
        if let now = reader.now { text += "\(now.state == "paused" ? "Paused" : "Playing"): \(now.title) · " }
        text += "Plays on " + (reader.speakerNames.isEmpty
            ? (reader.kind == "homeassistant" ? "its Home Assistant's speakers" : "no speakers yet: choose them below") : reader.speakerNames.joined(separator: " + "))
        if !reader.isDefault && !reader.default.isEmpty { text += " (chosen on the reader)" }
        return text
    }

    /// The default speakers by name, for the menu's label.
    private var chosenSpeakers: String {
        let names = Dictionary(state.speakers.map { ($0.id, $0.name) }, uniquingKeysWith: { first, _ in first })
        let list = reader.default.map { names[$0] ?? "\($0) (away)" }
        return list.isEmpty ? (reader.kind == "homeassistant" ? "Its Home Assistant's" : "No speakers") : list.joined(separator: " + ")
    }

    private func next(_ id: String, _ on: Bool) -> [String] {
        on ? reader.default.filter { $0 != id } + [id] : reader.default.filter { $0 != id }
    }

    private func savePage() {
        let trimmed = page.trimmingCharacters(in: .whitespaces)
        guard trimmed.isEmpty || Int(trimmed) != nil else { model.say("A Tessera page is a number", error: true); return }
        send("readers/\(path)", ["page": Int(trimmed).map { $0 as Any } ?? NSNull()], done: "\(reader.name) saved")
    }

    private func send(_ route: String, _ body: [String: Any], done: String?) {
        busy = true
        Task {
            do {
                let next: ReadersState = try await model.api.post(route, body)
                update(next)
                if let done { model.say(done) }
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            busy = false
        }
    }
}
