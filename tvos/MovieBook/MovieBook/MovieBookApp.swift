import AVFoundation
import SwiftUI

/// Marquee: browse and play your home movie library, and start movies from
/// NFC taps on a printed poster book.
///
/// The library comes from a Movie Book server on the home network, which this
/// Apple TV, iPhone or iPad pairs with once. Taps on the book reach the app through the
/// server's inbox, which the app checks while it's open. Without a server, a
/// small demo library of free films shows how it works.
@main
struct MovieBookApp: App {
    @StateObject private var app = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    #if os(iOS)
    init() {
        // Sound even with the ring/silent switch on silent, as other video apps have it
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
    }
    #endif

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                .environmentObject(app.player)
                .environmentObject(app.library)
                .onOpenURL { app.player.handle(url: $0) }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in  // initial: a launch starts out active
            if phase == .active {
                app.player.becameActive()
                #if os(tvOS)  // servers only open Marquee (and expect it) on Apple TVs
                app.startPairingWithExpectingServers()
                #endif
            } else {
                app.player.resignedActive()
                #if os(tvOS)
                app.stopPairingWithExpectingServers()
                #endif
            }
        }
    }
}
