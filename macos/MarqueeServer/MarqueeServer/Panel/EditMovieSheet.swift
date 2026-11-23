import SwiftUI

/// Match a folder movie with TMDB, or change its details by hand. Nothing is written to the share.
struct EditMovieSheet: View {
    let model: PanelModel
    let movie: Movie
    @Environment(\.dismiss) private var dismiss
    @State private var fileId = ""
    @State private var details: FolderMovie?
    @State private var error: String?
    @State private var busy = false
    @State private var tmdbReady: Bool?
    @State private var version = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Edit \(movie.title)").font(.title2.weight(.semibold))
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding([.horizontal, .top], 20)
            .padding(.bottom, 12)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if movie.files.count > 1 {
                        Picker("File", selection: $fileId) {
                            ForEach(movie.files, id: \.id) { Text($0.source).tag($0.id) }
                        }
                        .frame(maxWidth: 320)
                    }
                    if let error { ErrorText(error) }
                    if let details {
                        header(details)
                        Divider()
                        Text("Match with TMDB").font(.headline)
                        if tmdbReady == false {
                            Text("Add a TMDB API key on the Sources tab to look movies up.").foregroundStyle(.secondary)
                        } else {
                            TmdbSearch(model: model, title: details.file.title ?? details.title,
                                       year: details.file.year ?? details.year, current: details.ids["tmdb"], busy: busy) { result in
                                save("match", ["tmdb_id": result.id],
                                     done: "Matched with \(result.title)\(result.year.map { " (\($0))" } ?? "")")
                            }
                            .id(fileId)
                        }
                        Divider()
                        Text("Details").font(.headline)
                        DetailsForm(movie: details, busy: busy) { body in save("edit", body, done: "Saved") }
                            .id("\(fileId)-\(version)")
                    } else if error == nil {
                        ProgressView().frame(maxWidth: .infinity)
                    }
                }
                .padding(20)
            }
        }
        .frame(width: 720, height: 680)
        .task(id: fileId) { await load() }
        .task {
            if fileId.isEmpty { fileId = movie.files.first?.id ?? "" }
            if let sources: SourcesState = try? await model.api.get("sources") { tmdbReady = sources.tmdb }
        }
    }

    private func header(_ m: FolderMovie) -> some View {
        HStack(alignment: .top, spacing: 16) {
            Poster(url: model.api.poster(m.key, 160, 240, version: "\(m.matched)\(m.ids["tmdb"] ?? "")\(version)"), width: 110)
            VStack(alignment: .leading, spacing: 6) {
                Text("\(m.title)\(m.year.map { " (\($0))" } ?? "")").font(.title3.weight(.semibold))
                HStack(spacing: 4) {
                    CodeText(m.path)
                    Text("in \(m.source)").foregroundStyle(.secondary)
                }
                HStack {
                    if !m.matched.isEmpty {
                        Pill(text: "Matched with TMDB\(m.matched == "auto" ? " (automatically)" : "")", tone: .good)
                    } else if m.ids["tmdb"] != nil || m.ids["imdb"] != nil {
                        Pill(text: "IDs from its .nfo or name", tone: .good)
                    } else if m.key.hasPrefix("t-") {
                        Pill(text: "Not matched", tone: .bad)
                    } else {
                        Pill(text: "Same movie as in your other sources", tone: .good)
                    }
                    if let tmdb = m.ids["tmdb"], let url = URL(string: "https://www.themoviedb.org/movie/\(tmdb)") {
                        Link("TMDB \(tmdb)", destination: url)
                    }
                }
                Text("Its file says: \(m.file.title ?? "")\(m.file.year.map { " (\($0))" } ?? "")").foregroundStyle(.secondary)
                if m.edited {
                    Button("Reset to the File") {
                        guard confirm("Go back to what the file says?", detail: "This forgets the match and any changes.",
                                      button: "Reset") else { return }
                        save("reset", [:], done: "Back to what the file says")
                    }
                    .disabled(busy)
                }
            }
        }
    }

    private func load() async {
        guard !fileId.isEmpty else { return }
        struct Answer: Decodable { let movie: FolderMovie }
        do {
            let answer: Answer = try await model.api.get("files/\(fileId)")
            details = answer.movie
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save(_ action: String, _ body: [String: Any], done: String) {
        busy = true
        Task {
            struct Answer: Decodable { let movie: FolderMovie }
            do {
                let answer: Answer = try await model.api.post("files/\(fileId)/\(action)", body)
                details = answer.movie
                version += 1
                model.libraryChanged()
                model.say(done)
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            busy = false
        }
    }
}

private struct TmdbSearch: View {
    let model: PanelModel
    let current: String?
    let busy: Bool
    let onUse: (TmdbResult) -> Void
    @State private var query: String
    @State private var year: String
    @State private var results: [TmdbResult]?
    @State private var looking = false
    @State private var error: String?

    init(model: PanelModel, title: String, year: Int?, current: String?, busy: Bool, onUse: @escaping (TmdbResult) -> Void) {
        self.model = model
        self.current = current
        self.busy = busy
        self.onUse = onUse
        _query = State(initialValue: title)
        _year = State(initialValue: year.map(String.init) ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                TextField("Title", text: $query).textFieldStyle(.roundedBorder).frame(maxWidth: 280).onSubmit(search)
                TextField("Year", text: $year).textFieldStyle(.roundedBorder).frame(width: 70).onSubmit(search)
                Button("Search", action: search).disabled(query.trimmingCharacters(in: .whitespaces).isEmpty)
                if looking { ProgressView().controlSize(.small) }
            }
            if let error { ErrorText(error) }
            if let results, results.isEmpty { Text("TMDB has nothing by that name.").foregroundStyle(.secondary) }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 300), spacing: 8)], spacing: 8) {
                ForEach(results ?? []) { result in
                    let isCurrent = String(result.id) == current
                    HStack(alignment: .top, spacing: 10) {
                        Poster(url: result.poster.flatMap(URL.init(string:)), width: 54)
                        VStack(alignment: .leading, spacing: 4) {
                            Text("**\(result.title)**\(result.year.map { " (\($0))" } ?? "")")
                            Text(result.summary).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                            if isCurrent {
                                Pill(text: "Its match", tone: .good)
                            } else {
                                Button("Use This") { onUse(result) }.buttonStyle(.borderedProminent).controlSize(.small)
                                    .disabled(busy)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(8)
                    .overlay(RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(isCurrent ? Color.green : Color(nsColor: .separatorColor)))
                }
            }
        }
        .task { search() }
    }

    private func search() {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        looking = true
        Task {
            struct Answer: Decodable { let results: [TmdbResult] }
            do {
                let answer: Answer = try await model.api.get("match/search", ["q": q, "year": year.filter(\.isNumber)])
                results = answer.results
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
            looking = false
        }
    }
}

private struct DetailsForm: View {
    let busy: Bool
    let onSave: ([String: Any]) -> Void
    @State private var title: String
    @State private var year: String
    @State private var summary: String
    @State private var collections: String
    @State private var labels: String
    @State private var tmdb: String

    init(movie: FolderMovie, busy: Bool, onSave: @escaping ([String: Any]) -> Void) {
        self.busy = busy
        self.onSave = onSave
        _title = State(initialValue: movie.title)
        _year = State(initialValue: movie.year.map(String.init) ?? "")
        _summary = State(initialValue: movie.summary)
        _collections = State(initialValue: movie.collections.joined(separator: ", "))
        _labels = State(initialValue: movie.labels.joined(separator: ", "))
        _tmdb = State(initialValue: movie.ids["tmdb"] ?? "")
    }

    var body: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 10) {
            field("Title") { TextField("Title", text: $title) }
            field("Year") { TextField("Year", text: $year, prompt: Text("1979")).frame(width: 80) }
            field("Summary") { TextField("Summary", text: $summary, axis: .vertical).lineLimit(3...6) }
            field("Collections") { TextField("Collections", text: $collections, prompt: Text("Separated by commas")) }
            field("Labels") { TextField("Labels", text: $labels, prompt: Text("Separated by commas")) }
            field("TMDB ID") {
                VStack(alignment: .leading, spacing: 4) {
                    TextField("TMDB ID", text: $tmdb, prompt: Text("348")).frame(width: 120)
                    Text("Changing the ID can change the movie's tag address. Tags and cards made from its file name keep working.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            GridRow {
                Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                Button("Save") {
                    onSave(["title": title, "year": year, "summary": summary, "collections": collections,
                            "labels": labels, "tmdb": tmdb])
                }
                .buttonStyle(.borderedProminent)
                .disabled(busy || title.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .textFieldStyle(.roundedBorder)
    }

    private func field<Content: View>(_ label: String, @ViewBuilder _ content: () -> Content) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary).gridColumnAlignment(.trailing)
            content().labelsHidden()
        }
    }
}
