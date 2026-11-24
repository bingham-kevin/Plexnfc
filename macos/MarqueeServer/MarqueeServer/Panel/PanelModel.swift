import AppKit
import Observation
import SwiftUI

enum PanelTab: String, CaseIterable, Identifiable {
    case home, devices, tags, music, cards, book, sources
    var id: String { rawValue }

    var label: String {
        switch self {
        case .home: "Now Playing"
        case .devices: "Devices"
        case .tags: "Tags"
        case .music: "Music"
        case .cards: "Cards"
        case .book: "Book"
        case .sources: "Sources"
        }
    }

    var symbol: String {
        switch self {
        case .home: "play.tv"
        case .devices: "appletv"
        case .tags: "tag"
        case .music: "music.note.list"
        case .cards: "creditcard"
        case .book: "book"
        case .sources: "externaldrive.connected.to.line.below"
        }
    }
}

/// What the control panel window shows, and the server it talks to.
@MainActor
@Observable
final class PanelModel {
    struct Notice: Identifiable, Equatable {
        let id = UUID()
        let text: String
        let isError: Bool
    }

    let server: ServerController
    var selection: PanelTab? = .home
    var overview: Overview?
    var reachable = true
    var notice: Notice?
    /// Goes up when movies change (a source added, a movie matched, a card linked), so lists load again
    var libraryVersion = 0
    /// Goes up when a poster may have changed, so posters load again
    var posterVersion = 0

    init(server: ServerController) {
        self.server = server
    }

    var api: PanelAPI { PanelAPI(base: server.panelURL) }
    var tab: PanelTab { selection ?? .home }
    var canManage: Bool { overview?.canManage ?? false }

    /// Keep the overview (pairing requests, what's playing) up to date while the window is open.
    func poll() async {
        while !Task.isCancelled {
            await refreshOverview()
            try? await Task.sleep(for: .seconds(2))
        }
    }

    func refreshOverview() async {
        guard server.state == .running else {
            reachable = false
            return
        }
        do {
            let fresh: Overview = try await api.get("overview")
            if fresh != overview { overview = fresh }
            reachable = true
        } catch {
            reachable = false
        }
    }

    func say(_ text: String, error: Bool = false) {
        let notice = Notice(text: text, isError: error)
        self.notice = notice
        Task {
            try? await Task.sleep(for: .seconds(error ? 6 : 3))
            if self.notice == notice { self.notice = nil }
        }
    }

    /// Run a change, report how it went, and refresh the overview. Returns whether it worked.
    @discardableResult
    func perform(_ done: String? = nil, _ action: () async throws -> Void) async -> Bool {
        do {
            try await action()
            if let done { say(done) }
            await refreshOverview()
            return true
        } catch {
            say(error.localizedDescription, error: true)
            return false
        }
    }

    func libraryChanged() {
        libraryVersion += 1
        posterVersion += 1
    }

    func poster(_ key: String, _ width: Int, _ height: Int) -> URL {
        api.poster(key, width, height, version: posterVersion == 0 ? "" : "\(posterVersion)")
    }

    // MARK: Plex sign-in

    var signingIn = false

    /// Sign in with Plex: open plex.tv in the browser, then wait for the sign-in to arrive.
    func signInToPlex() async {
        struct Start: Decodable { let pinId: Int; let authUrl: String }
        struct Check: Decodable { let signedIn: Bool }
        do {
            let start: Start = try await api.post("plex/login")
            guard let url = URL(string: start.authUrl) else { return }
            NSWorkspace.shared.open(url)
            signingIn = true
            defer { signingIn = false }
            let deadline = Date().addingTimeInterval(600)
            while Date() < deadline, !Task.isCancelled {
                try await Task.sleep(for: .seconds(2))
                let data = try? await api.raw("plex/check", ["id": "\(start.pinId)"])
                if let data, let check = try? JSONDecoder.snake.decode(Check.self, from: data), check.signedIn {
                    say("Signed in to Plex")
                    libraryChanged()
                    await refreshOverview()
                    return
                }
            }
            say("Signing in took too long. Try again.", error: true)
        } catch {
            say(error.localizedDescription, error: true)
        }
    }
}

extension JSONDecoder {
    static var snake: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}

/// Ask before doing something that can't be undone: a Mac alert, like the panel's confirm().
@MainActor
func confirm(_ message: String, detail: String? = nil, button: String, destructive: Bool = true) -> Bool {
    let alert = NSAlert()
    alert.messageText = message
    if let detail { alert.informativeText = detail }
    alert.addButton(withTitle: button)
    alert.addButton(withTitle: "Cancel")
    if destructive { alert.buttons[0].hasDestructiveAction = true }
    return alert.runModal() == .alertFirstButtonReturn
}

@MainActor
func copyToPasteboard(_ text: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
}
