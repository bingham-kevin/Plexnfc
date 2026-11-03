import AVKit
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: PlayerModel

    var body: some View {
        if let player = model.player {
            ZStack {
                PlayerView(player: player, onDismiss: model.stop)
                if !model.isReady {
                    StartingCard()
                        .transition(.opacity)
                }
            }
            .ignoresSafeArea()
            .animation(.easeOut(duration: 0.6), value: model.isReady)
        } else {
            WaitingView()
        }
    }
}

/// Shown when nothing is playing: what plexnfc should call this Apple TV, and where plexnfc is.
private struct WaitingView: View {
    @EnvironmentObject private var model: PlayerModel

    var body: some View {
        VStack(spacing: 40) {
            Image(systemName: "film.stack")
                .font(.system(size: 120))
            Text("Marquee")
                .font(.largeTitle.bold())
            Text(model.isConfigured ? "Tap a poster in the book to start a movie."
                                    : "Enter plexnfc's address to get started.")
                .font(.title3)
                .foregroundStyle(.secondary)

            Form {
                Section {
                    TextField("plexnfc address, e.g. 192.168.1.20:8080", text: $model.serverAddress)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                    TextField("This Apple TV's player name", text: $model.playerName)
                } footer: {
                    Text("In plexnfc, use the player app:\(model.playerName.isEmpty ? "NAME" : model.playerName)")
                }
            }
            .frame(maxWidth: 1100, maxHeight: 360)

            if let error = model.lastError {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 1100)
            }
        }
        .padding(80)
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
                Image(uiImage: poster)
                    .resizable()
                    .scaledToFill()
                    .blur(radius: 60)
                    .opacity(0.45)
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

        // Menu on the remote while playing goes back to the waiting screen
        func playerViewControllerShouldDismiss(_ controller: AVPlayerViewController) -> Bool {
            onDismiss()
            return true
        }
    }
}
