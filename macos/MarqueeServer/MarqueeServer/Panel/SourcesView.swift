import AppKit
import SwiftUI

/// Where movies come from (Plex, Jellyfin servers, folders of movie files) in the order they're used.
struct SourcesView: View {
    let model: PanelModel
    let overview: Overview
    @State private var state: SourcesState?
    @State private var error: String?
    @State private var adding = false
    @State private var editingKey = false
    @State private var busy = false

    /// Plex is listed while it's used (or nothing else is); Add Source brings it back once removed
    private var shown: [String] {
        guard let state else { return [] }
        return state.order.filter { $0 != "plex" || state.plex.enabled || state.sources.isEmpty }
    }

    var body: some View {
        PanelSection(title: "Movie sources", subtitle: "A movie in several sources is one movie with one tag address. It "
                     + "plays from the first source here that has it and that the player can play, and from the next if "
                     + "that one doesn't answer.", trailing: {
            if overview.canManage {
                Button { adding = true } label: { Label("Add Source", systemImage: "plus") }
                    .buttonStyle(.borderedProminent)
            }
        }) {
            if let error { ErrorText(error) }
            if let state {
                VStack(spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.element) { index, id in
                        if index > 0 { Divider() }
                        row(index: index, id: id, state: state)
                    }
                }
                .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
                if overview.canManage {
                    HStack(spacing: 8) {
                        Text("Matching folder movies with TMDB:").foregroundStyle(.secondary)
                        Pill(text: state.tmdb ? "Set up" : "Not set up", tone: state.tmdb ? .good : .plain)
                        Button(state.tmdb ? "Change" : "Set Up") { editingKey = true }.buttonStyle(.link)
                    }
                    .font(.callout)
                }
            } else if error == nil {
                ProgressView()
            }
        }
        .task(id: model.libraryVersion) { await load() }
        // Checked every second while folders are being matched with TMDB
        .task(id: state?.matching.values.contains { $0.running } ?? false) {
            while state?.matching.values.contains(where: \.running) == true, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                await load()
                if state?.matching.values.contains(where: \.running) == false { model.libraryChanged() }
            }
        }
        .sheet(isPresented: $adding) {
            if let state { AddSourceSheet(model: model, plex: state.plex) { self.state = $0 } }
        }
        .sheet(isPresented: $editingKey) {
            TmdbKeySheet(model: model, isSet: state?.tmdb ?? false) { self.state = $0 }
        }
    }

    @ViewBuilder private func row(index: Int, id: String, state: SourcesState) -> some View {
        HStack(alignment: .center, spacing: 12) {
            HStack(spacing: 2) {
                Text("\(index + 1)").monospacedDigit().frame(width: 18)
                if overview.canManage && shown.count > 1 {
                    Button { move(index, by: -1) } label: { Image(systemName: "chevron.up") }
                        .disabled(index == 0 || busy).help("Use it sooner")
                    Button { move(index, by: 1) } label: { Image(systemName: "chevron.down") }
                        .disabled(index == shown.count - 1 || busy).help("Use it later")
                }
            }
            .buttonStyle(.borderless)
            .frame(width: 70, alignment: .leading)
            if id == "plex" {
                PlexRow(model: model, plex: state.plex, canManage: overview.canManage, others: !state.sources.isEmpty) { self.state = $0 }
            } else if let source = state.sources.first(where: { $0.id == id }) {
                SourceRow(model: model, source: source, canManage: overview.canManage, tmdb: state.tmdb,
                          job: state.matching[source.id]) { self.state = $0 }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private func load() async {
        do {
            state = try await model.api.get("sources")
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func move(_ index: Int, by: Int) {
        guard let state else { return }
        var ids = shown
        ids.swapAt(index, index + by)
        let order = ids + state.order.filter { !ids.contains($0) }
        busy = true
        Task {
            await sourceAction(model, "sources/order", ["order": order], done: "Order saved") { self.state = $0 }
            busy = false
        }
    }
}

/// POST to a source route, whose answer is the new list of sources: the movie lists change too.
@MainActor
@discardableResult
func sourceAction(_ model: PanelModel, _ path: String, _ body: [String: Any] = [:], done: String? = nil,
                  update: @escaping (SourcesState) -> Void) async -> SourcesState? {
    do {
        let state: SourcesState = try await model.api.post(path, body)
        update(state)
        model.libraryChanged()
        if let done { model.say(done) }
        return state
    } catch {
        model.say(error.localizedDescription, error: true)
        return nil
    }
}

private struct PlexRow: View {
    let model: PanelModel
    let plex: PlexSource
    let canManage: Bool
    let others: Bool
    let update: (SourcesState) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack { Text("Plex").bold(); KindTag(text: "Plex") }
            Pill(text: plex.signedIn ? "Signed in · library \(plex.library)" : "Not signed in", tone: plex.signedIn ? .good : .bad)
        }
        Spacer()
        if canManage {
            if !plex.signedIn { PlexSignInButton(model: model) }
            if others {
                Button("Remove", role: .destructive) {
                    guard confirm("Stop using Plex's movies?", detail: "You can add Plex back with Add Source.",
                                  button: "Remove") else { return }
                    Task { await sourceAction(model, "sources/plex", ["enabled": false], done: "Removed Plex", update: update) }
                }
            }
        }
    }
}

private struct SourceRow: View {
    let model: PanelModel
    let source: MovieSource
    let canManage: Bool
    let tmdb: Bool
    let job: MatchJob?
    let update: (SourcesState) -> Void
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack { Text(source.name).bold(); KindTag(text: source.kindName) }
            Text(source.whereText).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
            if let error = source.error { ErrorText(error).font(.callout) }
            if let job {
                if job.running {
                    HStack {
                        ProgressView(value: Double(job.done), total: Double(max(job.total, 1))).frame(width: 160)
                        Text("Matching with TMDB: \(job.done) of \(job.total) (\(job.matched) matched)")
                    }
                    .font(.callout).foregroundStyle(.secondary)
                } else {
                    Text("Matched \(job.matched) of \(job.total) with TMDB.\(job.matched < job.total ? " Match the rest from the Tags tab." : "")")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if let error = job.error { ErrorText(error).font(.callout) }
            }
        }
        Spacer()
        Text(source.count.map { "\($0) movies" } ?? "—").foregroundStyle(.secondary).monospacedDigit()
        if canManage {
            HStack {
                if source.kind == "folder" && tmdb {
                    Button(job?.running == true ? "Matching…" : "Match All") {
                        run("sources/\(source.id)/match", done: "Matching \(source.name)'s movies with TMDB…")
                    }
                    .disabled(busy || job?.running == true)
                    .help("Match its movies that have no ID yet, when TMDB has one with the same title and year")
                }
                Button(busy ? "Reading…" : "Rescan") { rescan() }
                    .disabled(busy)
                    .help("Read its movies again now, e.g. after adding files")
                Button("Remove", role: .destructive) {
                    guard confirm("Remove \(source.name)?", detail: "Its movies stay playable from the other sources that have them.",
                                  button: "Remove") else { return }
                    run("sources/\(source.id)/remove", done: "Removed \(source.name)")
                }
                .disabled(busy)
            }
        }
    }

    private func run(_ path: String, done: String) {
        busy = true
        Task {
            await sourceAction(model, path, done: done, update: update)
            busy = false
        }
    }

    private func rescan() {
        busy = true
        Task {
            if let state = await sourceAction(model, "sources/\(source.id)/refresh", update: update) {
                model.say("\(source.name): \(state.count ?? 0) movies")
            }
            busy = false
        }
    }
}

// MARK: - Adding a source

private enum SourceKind: String, CaseIterable, Identifiable {
    case folder, jellyfin, plex
    var id: String { rawValue }
    var label: String {
        switch self {
        case .folder: "Network or local folder"
        case .jellyfin: "Jellyfin server"
        case .plex: "Plex server"
        }
    }
}

/// Add a source: choose its kind, then fill in what that kind needs.
private struct AddSourceSheet: View {
    let model: PanelModel
    let plex: PlexSource
    let update: (SourcesState) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var kind: SourceKind = .folder

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Add a Source").font(.title2.weight(.semibold))
            Picker("Type", selection: $kind) {
                ForEach(SourceKind.allCases) { Text($0.label).tag($0) }
            }
            .frame(maxWidth: 360)
            Divider()
            switch kind {
            case .folder: AddFolderForm(model: model, update: added)
            case .jellyfin: AddJellyfinForm(model: model, update: added)
            case .plex: AddPlexForm(model: model, plex: plex, update: added)
            }
            Spacer(minLength: 0)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 520)
        .frame(minHeight: 380)
    }

    private func added(_ state: SourcesState) {
        update(state)
        dismiss()
    }
}

private struct AddFolderForm: View {
    let model: PanelModel
    let update: (SourcesState) -> Void
    @State private var path = ""
    @State private var name = ""
    @State private var busy = false
    @State private var showHelp = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            LabeledContent("Folder") {
                HStack {
                    TextField("Folder", text: $path, prompt: Text("/Volumes/Movies")).labelsHidden()
                    Button("Browse…", action: browse)
                }
            }
            Text("Mount a network share on this Mac first (Finder › Go › Connect to Server); it then shows up under Browse.")
                .font(.caption).foregroundStyle(.secondary)
            LabeledContent("Name") {
                TextField("Name", text: $name, prompt: Text("The folder's name")).labelsHidden()
            }
            HStack {
                Button(busy ? "Reading the folder…" : "Add") { add() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || path.trimmingCharacters(in: .whitespaces).isEmpty)
                if busy { ProgressView().controlSize(.small) }
            }
            DisclosureGroup("How movies are named", isExpanded: $showHelp) {
                Text("From their files or folders (Alien (1979)/Alien (1979).mkv), or a movie.nfo beside them. Posters "
                     + "come from a poster.jpg beside the movie or the cover inside the file, or from TMDB once matched. "
                     + "MP4, M4V and MOV play as they are; MKV, AVI and others need ffmpeg on this Mac. Folder movies "
                     + "play in Marquee only.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .textFieldStyle(.roundedBorder)
    }

    /// The server runs on this Mac, so its folders are this Mac's folders: pick one the usual way.
    private func browse() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use This Folder"
        panel.message = "Choose the folder that holds your movies"
        panel.directoryURL = URL(fileURLWithPath: path.isEmpty ? "/Volumes" : path)
        if panel.runModal() == .OK, let url = panel.url {
            path = url.path
        }
    }

    private func add() {
        busy = true
        Task {
            if let state = await sourceAction(model, "sources", ["kind": "folder", "path": path, "name": name],
                                              done: "Added the folder", update: { _ in }) {
                update(state)
            }
            busy = false
        }
    }
}

private struct AddJellyfinForm: View {
    let model: PanelModel
    let update: (SourcesState) -> Void
    @State private var url = ""
    @State private var name = ""
    @State private var library = ""
    @State private var useKey = false
    @State private var username = ""
    @State private var password = ""
    @State private var apiKey = ""
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            LabeledContent("Address") {
                TextField("Address", text: $url, prompt: Text("http://192.168.1.20:8096")).labelsHidden()
            }
            Picker("Connect with", selection: $useKey) {
                Text("Sign in").tag(false)
                Text("API key").tag(true)
            }
            .pickerStyle(.segmented)
            .fixedSize()
            if useKey {
                LabeledContent("API key") { TextField("API key", text: $apiKey).labelsHidden() }
                Text("From Jellyfin's Dashboard › API Keys.").font(.caption).foregroundStyle(.secondary)
            } else {
                LabeledContent("User name") {
                    TextField("User name", text: $username).labelsHidden().textContentType(.username)
                }
                LabeledContent("Password") {
                    SecureField("Password", text: $password).labelsHidden().textContentType(.password)
                }
                Text("Only passed on to Jellyfin; the sign-in it hands back is kept, not the password.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            LabeledContent("Library") {
                TextField("Library", text: $library, prompt: Text("Every movie library")).labelsHidden()
            }
            LabeledContent("Name") { TextField("Name", text: $name, prompt: Text("Jellyfin")).labelsHidden() }
            HStack {
                Button(busy ? "Connecting…" : "Add") { add() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || url.isEmpty || (useKey ? apiKey.isEmpty : username.isEmpty))
                if busy { ProgressView().controlSize(.small) }
            }
        }
        .textFieldStyle(.roundedBorder)
    }

    private func add() {
        busy = true
        var body: [String: Any] = ["kind": "jellyfin", "url": url, "name": name, "library": library]
        if useKey {
            body["api_key"] = apiKey
        } else {
            body["username"] = username
            body["password"] = password
        }
        Task {
            if let state = await sourceAction(model, "sources", body, done: "Added the Jellyfin server", update: { _ in }) {
                password = ""
                update(state)
            }
            busy = false
        }
    }
}

/// Plex servers: the one signed in to (added back if it was removed), others on the Plex
/// account (its own and ones shared with it), or any server by its address and token.
private struct AddPlexForm: View {
    let model: PanelModel
    let plex: PlexSource
    let update: (SourcesState) -> Void
    @State private var servers: [PlexServerChoice]?
    @State private var error: String?
    @State private var library = ""
    @State private var name = ""
    @State private var url = ""
    @State private var token = ""
    @State private var adding: String?  // the machine ID being added, or "address"
    @State private var byAddress = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !plex.signedIn {
                HStack {
                    PlexSignInButton(model: model)
                    Text("to choose from your Plex servers").foregroundStyle(.secondary)
                }
            } else if !plex.enabled {
                HStack {
                    Button("Add Plex Back") { send(["enabled": true], path: "sources/plex", done: "Using Plex's movies") }
                        .buttonStyle(.borderedProminent)
                    Text("the server you're signed in to (library \(plex.library))").foregroundStyle(.secondary)
                }
            }
            if let error { ErrorText(error) }
            if plex.signedIn && servers == nil && error == nil {
                HStack { ProgressView().controlSize(.small); Text("Asking plex.tv for your servers…").foregroundStyle(.secondary) }
            }
            ForEach(servers ?? []) { server in
                HStack {
                    Text(server.name).bold()
                    Text(server.owned ? "yours" : "shared with you").foregroundStyle(.secondary)
                    Spacer()
                    if server.signedIn {
                        Pill(text: "Signed in (Plex)", tone: .good)
                    } else if server.added {
                        Pill(text: "Added", tone: .good)
                    } else {
                        Button(adding == server.machine ? "Connecting…" : "Add") {
                            adding = server.machine
                            send(["kind": "plex", "machine": server.machine, "library": library, "name": name],
                                 done: "Added \(server.name)")
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(adding != nil)
                    }
                }
                .padding(8)
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color(nsColor: .separatorColor)))
            }
            LabeledContent("Library") {
                TextField("Library", text: $library, prompt: Text("Every movie library")).labelsHidden()
            }
            LabeledContent("Name") {
                TextField("Name", text: $name, prompt: Text("The server's name")).labelsHidden()
            }
            DisclosureGroup("Add a server by its address", isExpanded: $byAddress) {
                VStack(alignment: .leading, spacing: 8) {
                    LabeledContent("Address") {
                        TextField("Address", text: $url, prompt: Text("http://192.168.1.20:32400")).labelsHidden()
                    }
                    LabeledContent("Token") {
                        TextField("Token", text: $token, prompt: Text("The server's X-Plex-Token")).labelsHidden()
                    }
                    Button(adding == "address" ? "Connecting…" : "Add") {
                        adding = "address"
                        send(["kind": "plex", "url": url, "token": token, "library": library, "name": name],
                             done: "Added the Plex server")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(adding != nil || url.isEmpty || token.isEmpty)
                }
                .padding(.top, 6)
            }
            Text("Movies from another Plex server play in Marquee, streamed straight from that server, so the Apple TV has to be able to reach it.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .textFieldStyle(.roundedBorder)
        .task(id: plex.signedIn) {
            guard plex.signedIn else { return }
            struct Answer: Decodable { let servers: [PlexServerChoice] }
            do {
                let answer: Answer = try await model.api.get("plex/servers")
                servers = answer.servers
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    private func send(_ body: [String: Any], path: String = "sources", done: String) {
        Task {
            if let state = await sourceAction(model, path, body, done: done, update: { _ in }) {
                update(state)
            }
            adding = nil
        }
    }
}

/// A TMDB API key, for matching folder movies and giving them posters and details.
private struct TmdbKeySheet: View {
    let model: PanelModel
    let isSet: Bool
    let update: (SourcesState) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var key = ""
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Matching Folder Movies").font(.title2.weight(.semibold))
            Text("Folder movies are matched with TMDB (The Movie Database, which Plex and Jellyfin use too) for their "
                 + "proper titles, posters, summaries and collections. That needs a free API key: sign up at "
                 + "themoviedb.org and copy the API key from Settings › API. Matches are kept on this Mac, not written to "
                 + "your files.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let url = URL(string: "https://www.themoviedb.org/settings/api") {
                Link("Open TMDB's API settings", destination: url)
            }
            if isSet { Pill(text: "A key is saved", tone: .good) }
            HStack {
                TextField("TMDB API key", text: $key, prompt: Text(isSet ? "A new TMDB API key" : "TMDB API key"))
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(save)
                Button(busy ? "Checking…" : "Save", action: save)
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || key.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            Text("Then use Match All on a folder, or Match… beside a movie on the Tags tab.")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                if isSet {
                    Button("Remove Key", role: .destructive) {
                        guard confirm("Remove the TMDB key?", detail: "Matches made already stay.", button: "Remove") else { return }
                        send("", done: "TMDB key removed")
                    }
                }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 500)
    }

    private func save() {
        let trimmed = key.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        send(trimmed, done: "TMDB key saved")
    }

    private func send(_ value: String, done: String) {
        busy = true
        Task {
            if await sourceAction(model, "sources/tmdb", ["key": value], done: done, update: update) != nil {
                dismiss()
            }
            busy = false
        }
    }
}
