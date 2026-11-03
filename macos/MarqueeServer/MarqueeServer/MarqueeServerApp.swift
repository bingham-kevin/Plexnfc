import AppKit
import ServiceManagement
import SwiftUI

/// Marquee Server: plexnfc as a Mac menu bar app, so the movie book works without
/// Home Assistant. It serves the tag addresses, and opens Marquee on paired Apple
/// TVs itself when a tag is tapped.
@main
struct MarqueeServerApp: App {
    @NSApplicationDelegateAdaptor private var delegate: AppDelegate

    var body: some Scene {
        MenuBarExtra("Marquee Server", systemImage: "film.stack") {
            MenuContent(server: delegate.server)
        }
        Window("Marquee Server Settings", id: "settings") {
            SettingsView(server: delegate.server)
        }
        .windowResizability(.contentSize)
        Window("Marquee Server Log", id: "log") {
            LogView(server: delegate.server)
        }
    }
}

/// Starts the server with the app, not when the menu is first opened, and stops it on quit.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let server = ServerController()

    func applicationDidFinishLaunching(_ notification: Notification) {
        server.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        server.stop()
    }
}

struct MenuContent: View {
    @ObservedObject var server: ServerController
    @Environment(\.openWindow) private var openWindow
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled

    var body: some View {
        Text(server.statusText)
        Divider()
        Button("Open Control Panel") { NSWorkspace.shared.open(server.panelURL) }
            .disabled(server.state != .running)
        switch server.state {
        case .running: Button("Restart Server") { server.restart() }
        case .settingUp: EmptyView()
        default: Button("Start Server") { server.start() }
        }
        Divider()
        Button("Settings…") { show("settings") }
            .keyboardShortcut(",")
        Button("Show Log") { show("log") }
        Button("Open Data Folder") { NSWorkspace.shared.open(server.supportDirectory) }
        Toggle("Open at Login", isOn: $launchAtLogin)
            .onChange(of: launchAtLogin) { _, on in
                do {
                    if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                } catch {
                    launchAtLogin = SMAppService.mainApp.status == .enabled
                }
            }
        Divider()
        Button("Quit Marquee Server") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func show(_ window: String) {
        openWindow(id: window)
        NSApp.activate()
    }
}

struct SettingsView: View {
    @ObservedObject var server: ServerController

    var body: some View {
        Form {
            Section {
                TextField("Plex server", text: $server.plexURL, prompt: Text("Found when you sign in to Plex"))
                TextField("Movie library", text: $server.library)
                TextField("Marquee player", text: $server.appPlayer, prompt: Text("e.g. Living Room"))
            } footer: {
                Text("Sign in to Plex, pair Apple TVs and phones, and make the book in the control panel. "
                     + "Leave Plex server blank to use the server on your Plex account. "
                     + "The Marquee player is where taps play unless a phone or tag picks another.")
            }
            Section {
                TextField("Tag address", text: $server.tagAddress, prompt: Text(ServerController.defaultTagHost))
                TextField("Port", value: $server.port, format: .number.grouping(.never))
            } footer: {
                Text("Tags are written with \(server.publicBaseURL)/m/…. Phones find this Mac by that name "
                     + "wherever your router puts it, so it needs no fixed address. If you have two Marquee "
                     + "Servers, give each its own name ending in .local.")
            }
            Section {
                TextField("Python", text: $server.pythonPath, prompt: Text("Find automatically"))
            }
            HStack {
                Spacer()
                Button("Save and Restart") { server.restart() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .formStyle(.grouped)
        .frame(width: 460)
    }
}

struct LogView: View {
    @ObservedObject var server: ServerController

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(server.log.enumerated()), id: \.offset) { index, line in
                        Text(line).id(index)
                    }
                }
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: server.log.count) { _, count in proxy.scrollTo(count - 1, anchor: .bottom) }
        }
        .frame(minWidth: 560, minHeight: 320)
    }
}
