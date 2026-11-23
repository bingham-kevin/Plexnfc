import AppKit
import SwiftUI

/// The printable movie book: chosen here, opened in the browser to print.
struct BookView: View {
    let model: PanelModel
    let overview: Overview
    @AppStorage("book.title") private var title = "Movie Night"
    @AppStorage("book.collection") private var collection = ""
    @AppStorage("book.layout") private var layout = "full"
    @AppStorage("book.paper") private var paper = "letter"
    @AppStorage("book.qr") private var qr = true
    @State private var source = ""
    @State private var filters: Filters?

    var body: some View {
        PanelSection("Make the book") {
            HStack(alignment: .top, spacing: 28) {
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 12) {
                    GridRow {
                        Text("Title").foregroundStyle(.secondary).gridColumnAlignment(.trailing)
                        TextField("Title", text: $title).labelsHidden().frame(width: 260)
                    }
                    GridRow {
                        Text("Collection").foregroundStyle(.secondary)
                        HStack(spacing: 4) {
                            TextField("All movies", text: $collection).labelsHidden().frame(width: 230)
                            Menu {
                                Button("All movies") { collection = "" }
                                Divider()
                                ForEach(filters?.collections ?? [], id: \.title) { c in Button(c.title) { collection = c.title } }
                            } label: {
                                Image(systemName: "chevron.down")
                            }
                            .menuIndicator(.hidden)
                            .fixedSize()
                        }
                    }
                    if let sources = filters?.sources, !sources.isEmpty {
                        GridRow {
                            Text("From").foregroundStyle(.secondary)
                            Picker("From", selection: $source) {
                                Text("Every source").tag("")
                                ForEach(sources, id: \.id) { Text($0.name).tag($0.id) }
                            }
                            .labelsHidden().frame(width: 260)
                        }
                    }
                    GridRow {
                        Text("Layout").foregroundStyle(.secondary)
                        Picker("Layout", selection: $layout) {
                            Text("1 poster per page").tag("full")
                            Text("4 posters per page").tag("grid")
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
                        Toggle("QR code on each page, for phones without NFC", isOn: $qr)
                    }
                    GridRow {
                        Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                        Button("Open Printable Book") { NSWorkspace.shared.open(bookURL) }
                            .buttonStyle(.borderedProminent)
                    }
                }
                preview
            }
            Text("It opens in your browser. Wait for all the posters to load, then print at 100% scale with background "
                 + "graphics on. The book sets its own margins.")
                .foregroundStyle(.secondary)
        }
        .task(id: model.libraryVersion) { filters = try? await model.api.get("filters") }
    }

    private var bookURL: URL {
        model.api.url("book", ["title": title, "collection": collection, "source": source, "layout": layout,
                               "page_size": paper, "qr": qr ? "1" : "0"])
    }

    /// A sketch of a page: one poster, or four.
    private var preview: some View {
        let ratio: CGFloat = paper == "A4" || paper == "A5" ? 1.414 : paper == "legal" ? 1.647 : 1.294
        let count = layout == "grid" ? 4 : 1
        return VStack(spacing: 6) {
            let page = RoundedRectangle(cornerRadius: 4).fill(Color.white).shadow(radius: 2)
                .frame(width: 150, height: 150 * ratio)
            page.overlay {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 6), count: count == 4 ? 2 : 1), spacing: 6) {
                    ForEach(0..<count, id: \.self) { _ in
                        RoundedRectangle(cornerRadius: 2).fill(Color.gray.opacity(0.35))
                            .aspectRatio(2 / 3, contentMode: .fit)
                            .overlay(alignment: .bottomTrailing) {
                                if qr { Rectangle().fill(Color.black.opacity(0.7)).frame(width: count == 4 ? 9 : 18, height: count == 4 ? 9 : 18).padding(3) }
                            }
                    }
                }
                .padding(12)
            }
            Text("\(count == 4 ? "4 per page" : "1 per page") · \(paper)").font(.caption).foregroundStyle(.secondary)
        }
    }
}
