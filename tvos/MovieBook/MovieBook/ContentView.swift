import AVKit
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: PlayerModel

    var body: some View {
        if let player = model.player {
            PlayerView(player: player, onDismiss: model.stop)
                .ignoresSafeArea()
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
            Text("Movie Book")
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
