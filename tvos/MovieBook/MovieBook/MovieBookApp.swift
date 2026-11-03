import SwiftUI

/// Marquee: browse and play your home movie library, and start movies from
/// NFC taps on a printed poster book.
///
/// The library comes from a Movie Book server on the home network, which this
/// Apple TV pairs with once. Taps on the book reach the app through the
/// server's inbox, which the app checks while it's open. Without a server, a
/// small demo library of free films shows how it works.
@main
struct MovieBookApp: App {
    @StateObject private var app = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                .environmentObject(app.player)
                .environmentObject(app.library)
                .onOpenURL { app.player.handle(url: $0) }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                app.player.becameActive()
            } else {
                app.player.resignedActive()
            }
        }
    }
}
