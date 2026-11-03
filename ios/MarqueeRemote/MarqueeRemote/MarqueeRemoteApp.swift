import SwiftUI

@main
struct MarqueeRemoteApp: App {
    @StateObject private var model = RemoteModel()

    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(model)
        }
    }
}
