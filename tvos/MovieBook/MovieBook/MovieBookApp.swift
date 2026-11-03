import SwiftUI

/// Movie Book: an Apple TV player that plexnfc can start remotely.
///
/// Plex for Apple TV can't be told to play from another device, and other
/// players ignore links. Home Assistant *can* open an app by name, so this one
/// asks plexnfc what to play whenever it's opened (and every couple of seconds
/// while it's open). It also accepts `moviebook://play?url=...` links.
@main
struct MovieBookApp: App {
    @StateObject private var model = PlayerModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .onOpenURL { model.handle(url: $0) }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                model.becameActive()
            } else {
                model.resignedActive()
            }
        }
    }
}
