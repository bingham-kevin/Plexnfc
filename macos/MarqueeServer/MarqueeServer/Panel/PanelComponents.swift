import SwiftUI

/// A titled block of the panel, like the web panel's cards.
struct PanelSection<Content: View, Trailing: View>: View {
    let title: String
    var subtitle: String?
    var attention = false
    @ViewBuilder var trailing: () -> Trailing
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.title3.weight(.semibold))
                Spacer()
                trailing()
            }
            if let subtitle {
                Text(subtitle).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .strokeBorder(attention ? Color.accentColor : Color(nsColor: .separatorColor), lineWidth: attention ? 1.5 : 1))
    }
}

extension PanelSection where Trailing == EmptyView {
    init(_ title: String, subtitle: String? = nil, attention: Bool = false, @ViewBuilder content: @escaping () -> Content) {
        self.init(title: title, subtitle: subtitle, attention: attention, trailing: { EmptyView() }, content: content)
    }
}

/// A coloured dot and a few words: signed in, not matched…
struct Pill: View {
    enum Tone { case good, bad, plain }
    let text: String
    var tone: Tone = .plain

    var body: some View {
        HStack(spacing: 5) {
            Circle().frame(width: 7, height: 7)
            Text(text)
        }
        .font(.callout)
        .foregroundStyle(tone == .good ? Color.green : tone == .bad ? Color.red : Color.secondary)
        .padding(.horizontal, 9)
        .padding(.vertical, 3)
        .background(.quaternary.opacity(0.6), in: Capsule())
    }
}

/// A small outlined label: Plex, Folder, Marquee…
struct KindTag: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 1)
            .overlay(Capsule().strokeBorder(Color(nsColor: .separatorColor)))
    }
}

/// A movie poster from the server, with a quiet placeholder while it loads.
struct Poster: View {
    let url: URL?
    var width: CGFloat = 40
    var height: CGFloat { width * 1.5 }

    var body: some View {
        AsyncImage(url: url, transaction: Transaction(animation: .easeIn(duration: 0.15))) { phase in
            if let image = phase.image {
                image.resizable().aspectRatio(contentMode: .fill)
            } else {
                Rectangle().fill(.quaternary)
                    .overlay { if phase.error != nil { Image(systemName: "film").foregroundStyle(.secondary) } }
            }
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: width > 60 ? 8 : 4))
    }
}

/// The notice at the bottom of the window after a change: "Saved", or what went wrong.
struct NoticeBanner: View {
    let notice: PanelModel.Notice

    var body: some View {
        Label(notice.text, systemImage: notice.isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
            .foregroundStyle(notice.isError ? Color.red : Color.primary)
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .background(.regularMaterial, in: Capsule())
            .shadow(radius: 6, y: 2)
            .padding(.bottom, 18)
            .transition(.move(edge: .bottom).combined(with: .opacity))
    }
}

/// Text that says something went wrong.
struct ErrorText: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
    }
}

/// A tag address or a path, monospaced and selectable.
struct CodeText: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).font(.system(.callout, design: .monospaced)).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
    }
}

func clock(_ seconds: Double) -> String {
    let s = max(0, Int(seconds))
    let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
}
