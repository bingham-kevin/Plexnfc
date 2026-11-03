import AVKit
import SwiftUI

struct RootView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var player: PlayerModel

    var body: some View {
        ZStack {
            switch app.stage {
            case .welcome:
                WelcomeView()
            case .pairing(let address, let code):
                PairingView(address: address, code: code)
            case .demo, .connected:
                MainView()
            }
            if let avPlayer = player.player {
                PlaybackView(avPlayer: avPlayer)
                    .transition(.opacity)
            }
        }
        .overlay(alignment: .top) {
            if let notice = player.notice {  // a movie started on another player
                Label(notice, systemImage: "play.tv")
                    .font(.callout.weight(.semibold))
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(.regularMaterial, in: Capsule())
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.default, value: player.notice)
        .alert("Can't play", isPresented: Binding(get: { player.playError != nil },
                                                  set: { if !$0 { player.playError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(player.playError ?? "")
        }
    }
}

/// Sizes for a TV across the room, or a phone or tablet in the hand.
private enum Metrics {
    #if os(tvOS)
    static let margin: CGFloat = 80
    static let spacing: CGFloat = 50
    static let logo: CGFloat = 260
    static let codeSize: CGFloat = 140
    static let rowPoster: CGFloat = 260
    static let rowSpacing: CGFloat = 40
    static let gridSpacing: CGFloat = 60
    static let columns = Array(repeating: GridItem(.fixed(260), spacing: 48), count: 6)
    static let posterCorner: CGFloat = 0  // the card style rounds them
    static let fieldWidth: CGFloat = 1000
    #else
    static let margin: CGFloat = 16
    static let spacing: CGFloat = 24
    static let logo: CGFloat = 140
    static let codeSize: CGFloat = 56
    static let rowPoster: CGFloat = 120
    static let rowSpacing: CGFloat = 12
    static let gridSpacing: CGFloat = 16
    static let columns = [GridItem(.adaptive(minimum: 105, maximum: 180), spacing: 12)]
    static let posterCorner: CGFloat = 8
    static let fieldWidth: CGFloat = 480
    #endif
}

// MARK: - Setup

private struct WelcomeView: View {
    @EnvironmentObject private var app: AppModel
    @StateObject private var browser = ServerBrowser()
    @State private var address = ""

    var body: some View {
        GeometryReader { window in
            ScrollView {
                VStack(spacing: Metrics.spacing) {
                    Image("Marquee")
                        .resizable().scaledToFit().frame(height: Metrics.logo)
                        .clipShape(RoundedRectangle(cornerRadius: 30))
                        .accessibilityHidden(true)
                    Text("Marquee").font(.largeTitle.bold())
                    Text("Browse and play your home movie library, and start movies by tapping posters in a printed movie book.")
                        .font(.title3).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        .frame(maxWidth: 1100)
                        .fixedSize(horizontal: false, vertical: true)  // wrap, don't cut off

                    VStack(spacing: 20) {
                        // Servers found on the network: one click instead of typing the address
                        ForEach(browser.servers) { server in
                            Button("Connect to \(server.name) (\(server.address))") { app.pair(address: server.address) }
                        }
                        TextField(browser.servers.isEmpty ? "Movie Book server address, e.g. 192.168.1.20:8080"
                                                          : "Or enter another server's address", text: $address)
                            .keyboardType(.URL)
                            .textContentType(.URL)
                            #if os(iOS)
                            .textFieldStyle(.roundedBorder)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            #endif
                            .frame(maxWidth: Metrics.fieldWidth)
                            .onSubmit(connect)  // "done" on the keyboard connects straight away
                        HStack(spacing: 40) {
                            Button("Connect", action: connect)
                                .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                            Button("Try the Demo Library") { app.startDemo() }
                        }
                    }
                    #if os(iOS)
                    .buttonStyle(.bordered)
                    #endif
                    if let error = app.setupError {
                        Text(error).foregroundStyle(.red).multilineTextAlignment(.center).frame(maxWidth: 1100)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(Metrics.margin)
                .frame(maxWidth: .infinity, minHeight: window.size.height)  // centered when it fits
            }
        }
        .onAppear {
            if address.isEmpty { address = app.savedAddress }
            browser.start()
        }
        .onDisappear { browser.stop() }
    }

    private func connect() {
        guard !address.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        app.pair(address: address)
    }
}

private struct PairingView: View {
    @EnvironmentObject private var app: AppModel
    let address: String
    let code: String

    private var spacedCode: String { code.map(String.init).joined(separator: " ") }

    var body: some View {
        VStack(spacing: Metrics.spacing) {
            Text("Pair this \(app.deviceKind)").font(.largeTitle.bold())
            Text("Open your Movie Book server's page and approve this code.")
                .font(.title3).foregroundStyle(.secondary).multilineTextAlignment(.center)
            Text(spacedCode)
                .font(.system(size: Metrics.codeSize, weight: .bold, design: .monospaced))
                .minimumScaleFactor(0.5)
                .lineLimit(1)
                .accessibilityLabel("Pairing code \(spacedCode)")
            HStack(spacing: 16) {
                ProgressView()
                Text("Waiting for approval from \(address)…").foregroundStyle(.secondary)
            }
            Button("Cancel") { app.cancelPairing() }
        }
        .padding(Metrics.margin)
    }
}

// MARK: - Browsing

private struct MainView: View {
    var body: some View {
        TabView {
            HomeView().titled("Home").tabItem { tab("Home", "house") }
            MoviesView().titled("Movies").tabItem { tab("Movies", "film") }
            SearchView().titled("Search").tabItem { Label("Search", systemImage: "magnifyingglass") }
            SettingsView().titled("Settings").tabItem { tab("Settings", "gearshape") }
        }
    }

    /// Words alone on the TV's tab bar, as Apple's TV apps have them; icons too on a phone or tablet.
    @ViewBuilder private func tab(_ title: String, _ symbol: String) -> some View {
        #if os(tvOS)
        Text(title)
        #else
        Label(title, systemImage: symbol)
        #endif
    }
}

private extension View {
    /// A title bar on a phone or tablet (which also holds the search field); the TV's tab bar is enough there.
    @ViewBuilder func titled(_ title: String) -> some View {
        #if os(tvOS)
        self
        #else
        NavigationStack {
            self.navigationTitle(title)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) { PlayOnMenu() }
                    ToolbarItem(placement: .topBarTrailing) { ScanButton() }
                }
        }
        #endif
    }
}

#if os(iOS)
/// Where movies play: on this iPhone or iPad, or on another player such as an Apple TV.
/// Posters chosen here, scanned codes and tags tapped with this device all go there.
private struct PlayOnMenu: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var player: PlayerModel

    var body: some View {
        if app.connection != nil {
            Menu {
                Section("Play on") {
                    choice("This \(app.deviceKind)", app.deviceKind == "iPad" ? "ipad" : "iphone", nil)
                    ForEach(player.remotePlayers) { remote in
                        choice(remote.name, remote.kind == "Home Assistant" ? "house" : "tv", remote.id)
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: player.playsOn == nil ? "iphone" : "tv")
                    Text(player.playsOnName ?? "This \(app.deviceKind)").lineLimit(1)
                }
                .padding(.horizontal, 4)
            }
            .accessibilityLabel("Play on \(player.playsOnName ?? "this \(app.deviceKind)")")
        }
    }

    private func choice(_ name: String, _ symbol: String, _ id: String?) -> some View {
        let chosen = player.playsOn?.caseInsensitiveCompare(id ?? "") == .orderedSame || (player.playsOn == nil && id == nil)
        return Button { Task { await player.playOn(id) } } label: {
            Label(name, systemImage: chosen ? "checkmark" : symbol)
        }
    }
}

/// The camera, for the QR codes in the movie book.
private struct ScanButton: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var player: PlayerModel
    @State private var scanning = false

    var body: some View {
        if app.connection != nil {
            Button { scanning = true } label: { Label("Scan a Movie", systemImage: "qrcode.viewfinder") }
                .fullScreenCover(isPresented: $scanning) {
                    BookScanner { code in Task { await player.play(scanned: code) } }
                }
        }
    }
}
#endif

private struct HomeView: View {
    @EnvironmentObject private var library: LibraryModel

    var body: some View {
        ScrollView(.vertical) {
            LazyVStack(alignment: .leading, spacing: Metrics.spacing) {
                if library.isDemo {
                    Text("Demo library: free films to try Marquee with. Connect a Movie Book server in Settings to see your own movies.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if let error = library.error, library.rows.isEmpty {
                    Text(error).foregroundStyle(.red)
                }
                ForEach(library.rows) { row in
                    VStack(alignment: .leading, spacing: 16) {
                        Text(row.title).font(.title2.bold())
                        ScrollView(.horizontal) {
                            LazyHStack(spacing: Metrics.rowSpacing) {
                                ForEach(row.items) { PosterButton(movie: $0).frame(width: Metrics.rowPoster) }
                            }
                            #if os(tvOS)
                            .padding(.vertical, 30)  // room for the focused poster to grow
                            #endif
                        }
                        .scrollClipDisabled()
                        .scrollIndicators(.hidden)
                    }
                }
            }
            .padding(.horizontal, Metrics.margin)
        }
        .task { await library.loadHome() }
    }
}

private struct MoviesView: View {
    @EnvironmentObject private var library: LibraryModel

    var body: some View {
        ScrollView {
            LazyVGrid(columns: Metrics.columns, spacing: Metrics.gridSpacing) {
                ForEach(library.allMovies) { movie in
                    PosterButton(movie: movie)
                        .task { await library.loadMoreIfNeeded(after: movie) }
                }
            }
            .padding(Metrics.margin)
            if library.isLoading { ProgressView() }
        }
        .task { await library.loadMoreIfNeeded() }
    }
}

private struct SearchView: View {
    @EnvironmentObject private var library: LibraryModel
    @State private var text = ""

    var body: some View {
        ScrollView {
            LazyVGrid(columns: Metrics.columns, spacing: Metrics.gridSpacing) {
                ForEach(library.searchResults) { PosterButton(movie: $0) }
            }
            .padding(Metrics.margin)
        }
        .searchable(text: $text, prompt: "Movies")
        .onChange(of: text) { _, new in library.search(new) }
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var library: LibraryModel
    @EnvironmentObject private var player: PlayerModel

    var body: some View {
        Form {
            if let connection = app.connection {
                Section("Movie Book server") {
                    LabeledContent("Address", value: connection.address)
                    LabeledContent("This \(app.deviceKind)", value: connection.name)
                    #if os(tvOS)
                    Text("Taps in the movie book for “\(connection.name)” start here while Marquee is open.")
                        .foregroundStyle(.secondary)
                    #else
                    Text("Movies you choose, scan or tap with this \(app.deviceKind) play on \(player.playsOnName ?? "this \(app.deviceKind)"). Change it with the button at the top left. A tapped tag opens in Safari first; the movie waits for Marquee to open.")
                        .foregroundStyle(.secondary)
                    #endif
                    Button("Disconnect and Forget Server", role: .destructive) { app.disconnect() }
                }
            } else {
                Section("Movie Book server") {
                    Text("You're using the demo library.").foregroundStyle(.secondary)
                    Button("Connect a Server") { app.disconnect() }
                }
            }
            Section("Privacy") {
                Text("Marquee talks only to your own server on your home network. It collects no data and has no accounts, ads or tracking.")
                    .foregroundStyle(.secondary)
            }
            if library.isDemo {
                Section("About the demo films") {
                    Text(DemoLibrary.credits).foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: 1200)
    }
}

/// A poster you can select to play the movie. It fills the width it's given, at a poster's shape.
private struct PosterButton: View {
    @EnvironmentObject private var player: PlayerModel
    let movie: Movie

    var body: some View {
        Button {
            Task { await player.play(movie) }
        } label: {
            Color.clear
                .aspectRatio(2 / 3, contentMode: .fit)
                .overlay { PosterImage(movie: movie) }
                .clipShape(RoundedRectangle(cornerRadius: Metrics.posterCorner))
        }
        #if os(tvOS)
        .buttonStyle(.card)
        #else
        .buttonStyle(.plain)
        #endif
        .accessibilityLabel(movie.year.map { "\(movie.title), \($0)" } ?? movie.title)
    }
}

private struct PosterImage: View {
    @EnvironmentObject private var app: AppModel
    let movie: Movie

    var body: some View {
        if movie.demoStream != nil, let name = movie.poster {
            Image(name).resizable().scaledToFill()
        } else if let path = movie.poster, let url = app.connection.flatMap({ ServerAPI(connection: $0).url(path) }) {
            AsyncImage(url: url) { image in
                image.resizable().scaledToFill()
            } placeholder: {
                Placeholder(title: movie.title)
            }
        } else {
            Placeholder(title: movie.title)
        }
    }

    private struct Placeholder: View {
        let title: String
        var body: some View {
            ZStack {
                Color.primary.opacity(0.08)
                Text(title).font(.headline).multilineTextAlignment(.center).padding()
            }
        }
    }
}

// MARK: - Playback

/// The movie, with its poster and title shown while it starts.
private struct PlaybackView: View {
    @EnvironmentObject private var player: PlayerModel
    let avPlayer: AVPlayer

    var body: some View {
        ZStack {
            #if os(tvOS)
            PlayerView(player: avPlayer, onDismiss: player.stop)
            #else
            // Apple's player has no close button when it's part of the screen, so this bar has one
            VStack(spacing: 0) {
                HStack {
                    Button("Done") { player.stop() }.fontWeight(.semibold)
                    Spacer()
                }
                .padding(.horizontal)
                .frame(height: 44)
                PlayerView(player: avPlayer)
            }
            .background(Color.black)
            .environment(\.colorScheme, .dark)
            #endif
            if !player.isReady {
                StartingCard().transition(.opacity)
            }
        }
        #if os(tvOS)
        .ignoresSafeArea()
        #else
        .background(Color.black.ignoresSafeArea())
        #endif
        .animation(.easeOut(duration: 0.6), value: player.isReady)
    }
}

/// Shown over the player while the stream starts: the poster, title and year.
private struct StartingCard: View {
    @EnvironmentObject private var model: PlayerModel

    #if os(tvOS)
    private let layout = AnyLayout(HStackLayout(spacing: 80))
    private let posterSize = CGSize(width: 400, height: 600)
    private let titleFont = Font.system(size: 76, weight: .bold)
    private let margin: CGFloat = 120
    #else
    private let layout = AnyLayout(VStackLayout(alignment: .leading, spacing: 24))
    private let posterSize = CGSize(width: 160, height: 240)
    private let titleFont = Font.title.bold()
    private let margin: CGFloat = 32
    #endif

    var body: some View {
        ZStack {
            Color.black
            if let poster = model.poster {
                // The poster, blurred, fills the screen behind the card
                Image(uiImage: poster).resizable().scaledToFill().blur(radius: 60).opacity(0.45)
            }
            layout {
                Group {
                    if let poster = model.poster {
                        Image(uiImage: poster).resizable().scaledToFit()
                    } else {
                        RoundedRectangle(cornerRadius: 24).fill(.white.opacity(0.08))
                            .overlay(Image(systemName: "film").font(.system(size: posterSize.width / 4)).foregroundStyle(.secondary))
                    }
                }
                .frame(width: posterSize.width, height: posterSize.height)
                .clipShape(RoundedRectangle(cornerRadius: posterSize.width / 16))
                .shadow(radius: 40)

                VStack(alignment: .leading, spacing: 20) {
                    Text(model.info.title ?? "Starting…")
                        .font(titleFont)
                        .lineLimit(3)
                    if let year = model.info.year {
                        Text(String(year)).font(.title2).foregroundStyle(.secondary)
                    }
                    HStack(spacing: 16) {
                        ProgressView()
                        Text("Starting").font(.title3).foregroundStyle(.secondary)
                    }
                    .padding(.top, margin / 4)
                }
                .frame(maxWidth: 900, alignment: .leading)
            }
            .padding(margin)
        }
        .ignoresSafeArea()
        .environment(\.colorScheme, .dark)
    }
}

/// Apple's own player, so the Siri Remote (or touch) works as usual.
private struct PlayerView: UIViewControllerRepresentable {
    let player: AVPlayer
    var onDismiss: () -> Void = {}

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let controller = AVPlayerViewController()
        controller.player = player
        controller.delegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {
        if controller.player !== player { controller.player = player }
    }

    func makeCoordinator() -> Coordinator { Coordinator(onDismiss: onDismiss) }

    final class Coordinator: NSObject, AVPlayerViewControllerDelegate {
        let onDismiss: () -> Void
        init(onDismiss: @escaping () -> Void) { self.onDismiss = onDismiss }

        #if os(tvOS)
        // Menu on the remote while playing goes back to browsing
        func playerViewControllerShouldDismiss(_ controller: AVPlayerViewController) -> Bool {
            onDismiss()
            return true
        }
        #endif
    }
}
