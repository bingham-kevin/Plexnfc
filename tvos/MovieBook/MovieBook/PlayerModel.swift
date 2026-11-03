import AVFoundation
import Combine
import Foundation
import UIKit

/// A command left for this Apple TV by plexnfc (`GET /tv/inbox`).
struct InboxCommand: Decodable {
    let id: Int
    let action: String       // play, pause, resume, stop, seek
    let url: String?         // play: an HLS (or MP4) stream address
    let title: String?
    let key: String?         // the tag key, e.g. tmdb-603, reported back in /tv/status
    let start: Double?       // play: seconds to start at
    let by: Double?          // seek: seconds to jump (negative goes back)
}

@MainActor
final class PlayerModel: ObservableObject {
    @Published var serverAddress: String {
        didSet { UserDefaults.standard.set(serverAddress, forKey: "serverAddress") }
    }
    @Published var playerName: String {
        didSet { UserDefaults.standard.set(playerName, forKey: "playerName") }
    }
    @Published private(set) var player: AVPlayer?
    @Published private(set) var title: String?
    @Published private(set) var lastError: String?

    private var key: String?
    private var lastCommandID: Int {
        get { UserDefaults.standard.integer(forKey: "lastCommandID") }
        set { UserDefaults.standard.set(newValue, forKey: "lastCommandID") }
    }
    private var pollTask: Task<Void, Never>?
    private var statusCount = 0

    init() {
        serverAddress = UserDefaults.standard.string(forKey: "serverAddress") ?? ""
        playerName = UserDefaults.standard.string(forKey: "playerName") ?? UIDevice.current.name
    }

    var isConfigured: Bool { baseURL != nil && !playerName.isEmpty }

    private var baseURL: URL? {
        var address = serverAddress.trimmingCharacters(in: .whitespaces)
        guard !address.isEmpty else { return nil }
        if !address.contains("://") { address = "http://" + address }
        if address.hasSuffix("/") { address.removeLast() }
        return URL(string: address)
    }

    // MARK: - App lifecycle

    func becameActive() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.poll()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func resignedActive() {
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: - moviebook://play?url=...&title=...&start=...

    func handle(url: URL) {
        guard url.scheme == "moviebook",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return }
        let value = { (name: String) in items.first { $0.name == name }?.value }
        switch url.host {
        case "play":
            guard let stream = value("url") else { return }
            play(stream: stream, title: value("title"), key: value("key"),
                 start: value("start").flatMap(Double.init) ?? 0)
        case "configure":  // moviebook://configure?server=192.168.1.20:8080&player=Den
            if let server = value("server") { serverAddress = server }
            if let name = value("player") { playerName = name }
        default:
            break
        }
    }

    // MARK: - plexnfc inbox

    private func poll() async {
        guard let base = baseURL, !playerName.isEmpty else { return }
        var inbox = URLComponents(url: base.appending(path: "tv/inbox"), resolvingAgainstBaseURL: false)!
        inbox.queryItems = [URLQueryItem(name: "player", value: playerName),
                            URLQueryItem(name: "after", value: String(lastCommandID))]
        do {
            let (data, response) = try await URLSession.shared.data(from: inbox.url!)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                let command = try JSONDecoder().decode(InboxCommand.self, from: data)
                lastCommandID = command.id
                run(command)
            }
            lastError = nil
        } catch {
            lastError = "Can't reach plexnfc at \(base.absoluteString): \(error.localizedDescription)"
        }
        // Tell plexnfc what's playing every few polls, and right after a change
        statusCount += 1
        if statusCount % 3 == 0 { await reportStatus() }
    }

    private func run(_ command: InboxCommand) {
        switch command.action {
        case "play":
            if let stream = command.url {
                play(stream: stream, title: command.title, key: command.key, start: command.start ?? 0)
            }
        case "pause":
            player?.pause()
        case "resume":
            player?.play()
        case "stop":
            stop()
        case "seek":
            if let player, let by = command.by {
                let target = max(0, player.currentTime().seconds + by)
                player.seek(to: CMTime(seconds: target, preferredTimescale: 600))
            }
        default:
            break
        }
        Task { await reportStatus() }
    }

    // MARK: - Playback

    func play(stream: String, title: String?, key: String?, start: Double) {
        guard let url = URL(string: stream) else {
            lastError = "That isn't a stream address: \(stream)"
            return
        }
        let item = AVPlayerItem(url: url)
        let player = AVPlayer(playerItem: item)
        // Start with subtitles off rather than following the system's choice
        player.appliesMediaSelectionCriteriaAutomatically = false
        Task {
            if let group = try? await item.asset.loadMediaSelectionGroup(for: .legible) {
                item.select(nil, in: group)
            }
        }
        if start > 0 {
            player.seek(to: CMTime(seconds: start, preferredTimescale: 600))
        }
        self.title = title
        self.key = key
        self.player = player
        player.play()
    }

    func stop() {
        player?.pause()
        player = nil
        title = nil
        key = nil
    }

    private func reportStatus() async {
        guard let base = baseURL, !playerName.isEmpty else { return }
        var status: [String: Any] = ["player": playerName, "state": "idle"]
        if let player, let item = player.currentItem {
            status["state"] = player.timeControlStatus == .paused ? "paused" : "playing"
            status["position"] = Int(player.currentTime().seconds.isFinite ? player.currentTime().seconds : 0)
            let duration = item.duration.seconds
            status["duration"] = duration.isFinite ? Int(duration) : 0
            status["title"] = title ?? ""
            status["key"] = key ?? ""
        }
        var request = URLRequest(url: base.appending(path: "tv/status"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: status)
        _ = try? await URLSession.shared.data(for: request)
    }
}
