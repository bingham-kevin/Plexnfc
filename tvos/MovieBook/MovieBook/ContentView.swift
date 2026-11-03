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
        .alert("Can't play", isPresented: Binding(get: { player.playError != nil },
                                                  set: { if !$0 { player.playError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(player.playError ?? "")
        }
    }
}

// MARK: - Setup

private struct WelcomeView: View {
    @EnvironmentObject private var app: AppModel
    @State private var address = ""

    var body: some View {
        VStack(spacing: 44) {
            Image("Marquee")
                .resizable().scaledToFit().frame(height: 260)
                .clipShape(RoundedRectangle(cornerRadius: 30))
                .accessibilityHidden(true)
            Text("Marquee").font(.largeTitle.bold())
            Text("Browse and play your home movie library, and start movies by tapping posters in a printed movie book.")
                .font(.title3).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: 1100)
                .fixedSize(horizontal: false, vertical: true)  // wrap, don't cut off

            VStack(spacing: 20) {
                TextField("Movie Book server address, e.g. 192.168.1.20:8080", text: $address)
                    .keyboardType(.URL)
                    .textContentType(.URL)
                    .frame(maxWidth: 1000)
                    .onSubmit(connect)  // "done" on the keyboard connects straight away
                HStack(spacing: 40) {
                    Button("Connect", action: connect)
                        .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button("Try the Demo Library") { app.startDemo() }
                }
            }
            if let error = app.setupError {
                Text(error).foregroundStyle(.red).multilineTextAlignment(.center).frame(maxWidth: 1100)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(80)
        .onAppear { if address.isEmpty { address = app.savedAddress } }
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
        VStack(spacing: 48) {
            Text("Pair this Apple TV").font(.largeTitle.bold())
            Text("Open your Movie Book server's page and approve this code.")
                .font(.title3).foregroundStyle(.secondary)
            Text(spacedCode)
                .font(.system(size: 140, weight: .bold, design: .monospaced))
                .accessibilityLabel("Pairing code \(spacedCode)")
            HStack(spacing: 16) {
                ProgressView()
                Text("Waiting for approval from \(address)…").foregroundStyle(.secondary)
            }
            Button("Cancel") { app.cancelPairing() }
        }
        .padding(80)
    }
}

// MARK: - Browsing

private struct MainView: View {
    var body: some View {
        TabView {
            HomeView().tabItem { Text("Home") }
            MoviesView().tabItem { Text("Movies") }
            SearchView().tabItem { Label("Search", systemImage: "magnifyingglass") }
            SettingsView().tabItem { Text("Settings") }
        }
    }
}

private struct HomeView: View {
    @EnvironmentObject private var library: LibraryModel

    var body: some View {
        ScrollView(.vertical) {
            LazyVStack(alignment: .leading, spacing: 50) {
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
                            LazyHStack(spacing: 40) {
                                ForEach(row.items) { PosterButton(movie: $0) }
                            }
                            .padding(.vertical, 30)
                        }
                        .scrollClipDisabled()
                    }
                }
            }
            .padding(.horizontal, 80)
        }
        .task { await library.loadHome() }
    }
}

private let posterColumns = Array(repeating: GridItem(.fixed(260), spacing: 48), count: 6)

private struct MoviesView: View {
    @EnvironmentObject private var library: LibraryModel

    var body: some View {
        ScrollView {
            LazyVGrid(columns: posterColumns, spacing: 60) {
                ForEach(library.allMovies) { movie in
                    PosterButton(movie: movie)
                        .task { await library.loadMoreIfNeeded(after: movie) }
                }
            }
            .padding(60)
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
            LazyVGrid(columns: posterColumns, spacing: 60) {
                ForEach(library.searchResults) { PosterButton(movie: $0) }
            }
            .padding(60)
        }
        .searchable(text: $text, prompt: "Movies")
        .onChange(of: text) { _, new in library.search(new) }
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var library: LibraryModel

    var body: some View {
        Form {
            if let connection = app.connection {
                Section("Movie Book server") {
                    LabeledContent("Address", value: connection.address)
                    LabeledContent("This Apple TV", value: connection.name)
                    Text("Taps in the movie book for “\(connection.name)” start here while Marquee is open.")
                        .foregroundStyle(.secondary)
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

/// A poster you can select to play the movie.
private struct PosterButton: View {
    @EnvironmentObject private var player: PlayerModel
    let movie: Movie

    var body: some View {
        Button {
            Task { await player.play(movie) }
        } label: {
            PosterImage(movie: movie)
                .frame(width: 260, height: 390)
                .clipped()
        }
        .buttonStyle(.card)
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
                Color.white.opacity(0.08)
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
            PlayerView(player: avPlayer, onDismiss: player.stop)
            if !player.isReady {
                StartingCard().transition(.opacity)
            }
        }
        .ignoresSafeArea()
        .animation(.easeOut(duration: 0.6), value: player.isReady)
    }
}

/// Shown over the player while the stream starts: the poster, title and year.
private struct StartingCard: View {
    @EnvironmentObject private var model: PlayerModel

    var body: some View {
        ZStack {
            Color.black
            if let poster = model.poster {
                // The poster, blurred, fills the screen behind the card
                Image(uiImage: poster).resizable().scaledToFill().blur(radius: 60).opacity(0.45)
            }
            HStack(spacing: 80) {
                Group {
                    if let poster = model.poster {
                        Image(uiImage: poster).resizable().scaledToFit()
                    } else {
                        RoundedRectangle(cornerRadius: 24).fill(.white.opacity(0.08))
                            .overlay(Image(systemName: "film").font(.system(size: 90)).foregroundStyle(.secondary))
                    }
                }
                .frame(width: 400, height: 600)
                .clipShape(RoundedRectangle(cornerRadius: 24))
                .shadow(radius: 40)

                VStack(alignment: .leading, spacing: 20) {
                    Text(model.info.title ?? "Starting…")
                        .font(.system(size: 76, weight: .bold))
                        .lineLimit(3)
                    if let year = model.info.year {
                        Text(String(year)).font(.title2).foregroundStyle(.secondary)
                    }
                    HStack(spacing: 16) {
                        ProgressView()
                        Text("Starting").font(.title3).foregroundStyle(.secondary)
                    }
                    .padding(.top, 30)
                }
                .frame(maxWidth: 900, alignment: .leading)
            }
            .padding(120)
        }
    }
}

/// Apple's own player, so the Siri Remote works as usual.
private struct PlayerView: UIViewControllerRepresentable {
    let player: AVPlayer
    let onDismiss: () -> Void

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

        // Menu on the remote while playing goes back to browsing
        func playerViewControllerShouldDismiss(_ controller: AVPlayerViewController) -> Bool {
            onDismiss()
            return true
        }
    }
}
