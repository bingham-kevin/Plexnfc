import AppKit
import SwiftUI

/// The printable book of albums or audiobooks, in the movie book's style: chosen here,
/// opened in the browser to print.
struct MediaBookView: View {
    let model: PanelModel
    let kind: MediaSection  // .music or .books
    @State private var title = ""
    @State private var source = ""  // music: "" is everything with a card
    @State private var what = ""  // music: album or playlist; books: "", author:ID or series:ID
    @State private var layout = "grid"
    @State private var paper = "letter"
    @State private var qr = true
    @State private var withCards = true
    @State private var sources: [MusicSourcesState.Source] = []
    @State private var authors: [LibItem] = []
    @State private var series: [LibItem] = []

    private var books: Bool { kind == .books }

    var body: some View {
        PanelSection("Make the book") {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 12) {
                GridRow {
                    Text("Title").foregroundStyle(.secondary).gridColumnAlignment(.trailing)
                    TextField("Title", text: $title).labelsHidden().frame(width: 260)
                }
                if books {
                    GridRow {
                        Text("Books").foregroundStyle(.secondary)
                        Picker("Books", selection: $what) {
                            Text("Every audiobook").tag("")
                            if !authors.isEmpty {
                                Section("By author") {
                                    ForEach(authors) { Text($0.name).tag("author:\(ref($0))") }
                                }
                            }
                            if !series.isEmpty {
                                Section("A series") {
                                    ForEach(series) { Text($0.name).tag("series:\(ref($0))") }
                                }
                            }
                        }
                        .labelsHidden().frame(width: 260)
                    }
                } else {
                    GridRow {
                        Text("From").foregroundStyle(.secondary)
                        Picker("From", selection: $source) {
                            Text("Everything with a card").tag("")
                            ForEach(sources) { Text($0.name).tag($0.id) }
                        }
                        .labelsHidden().frame(width: 260)
                    }
                    if !source.isEmpty {
                        GridRow {
                            Text("Pages for").foregroundStyle(.secondary)
                            Picker("Pages for", selection: $what) {
                                Text("Albums").tag("album")
                                Text("Playlists").tag("playlist")
                            }
                            .labelsHidden().frame(width: 260)
                        }
                    }
                }
                GridRow {
                    Text("Layout").foregroundStyle(.secondary)
                    Picker("Layout", selection: $layout) {
                        Text("1 cover per page").tag("full")
                        Text("4 covers per page").tag("grid")
                    }
                    .labelsHidden().frame(width: 260)
                }
                GridRow {
                    Text("Paper").foregroundStyle(.secondary)
                    Picker("Paper", selection: $paper) {
                        ForEach(["letter", "A4", "legal", "A5"], id: \.self) { Text($0 == "letter" ? "Letter" : $0.capitalized).tag($0) }
                    }
                    .labelsHidden().frame(width: 260)
                }
                GridRow {
                    Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                    Toggle("QR code for \(books ? "books" : "music") with a card, for phones", isOn: $qr)
                }
                if books || !source.isEmpty {
                    GridRow {
                        Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                        Toggle("Only \(books ? "books" : "ones") with a card linked", isOn: $withCards)
                    }
                }
                GridRow {
                    Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                    Button("Open Printable Book") { NSWorkspace.shared.open(bookURL) }.buttonStyle(.borderedProminent)
                }
            }
            Text("Each page has the cover and a gold circle where its card goes. Hold the circle on a Movie Remote and it plays "
                 + "on the card's speaker\(books ? ", picking up where you left off" : ""). Link cards under "
                 + "\(kind.label) › Cards first. The QR code plays the card from a phone without NFC. It opens in your browser: "
                 + "print at 100% scale with background graphics on.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .task {
            if title.isEmpty { title = books ? "Story Time" : "Music" }
            if !books && what.isEmpty { what = "album" }
            if books {
                authors = (try? await model.api.get("music/browse", ["source": "abs", "kind": "author", "limit": "500"], as: Items.self).items) ?? []
                series = (try? await model.api.get("music/browse", ["source": "abs", "kind": "series", "limit": "500"], as: Items.self).items) ?? []
            } else {
                sources = ((try? await model.api.get("music/sources", as: MusicSourcesState.self).sources) ?? [])
                    .filter { $0.searchOnly != true }
            }
        }
    }

    private func ref(_ item: LibItem) -> String { item.id.components(separatedBy: "|").last ?? "" }

    private var bookURL: URL {
        let cardsOnly = !books && source.isEmpty ? true : withCards
        var query = ["title": title, "layout": layout, "page_size": paper, "qr": qr ? "1" : "0",
                     "cards": cardsOnly ? "1" : "0", "what": what]
        if !books { query["source"] = source }
        return model.api.url("book/\(books ? "books" : "music")", query)
    }
}
