import AVFoundation
import Combine
import Foundation
import UIKit

/// A command left for this Apple TV by the server (`GET /tv/inbox`), e.g. from an NFC tap.
struct InboxCommand: Decodable {
    let id: Int
    let action: String       // play, pause, resume, stop, seek
    let url: String?         // play: an HLS (or MP4) stream address
    let title: String?
    let key: String?         // the movie's key, e.g. tmdb-603, reported back in /tv/status
    let start: Double?       // play: seconds to start at
    let by: Double?          // seek: seconds to jump (negative goes back)
    let year: Int?
    let summary: String?
    let poster: String?      // a path on the server
}

/// A stream ready to play: what `/tv/play` returns.
private struct PlayDetails: Decodable {
    let url: String
    let start: Double?
}

/// What the player shows about the movie: on the starting card and in the info panel.
struct MovieInfo {
    var title: String?
    var year: Int?
    var summary: String?
    var posterURL: URL?
    var posterImage: UIImage?   // the demo library's posters are bundled
}

/// Playback, and the server's inbox for remote starts while paired.
@MainActor
final class PlayerModel: ObservableObject {
    @Published private(set) var player: AVPlayer?
    @Published private(set) var info = MovieInfo()
    @Published private(set) var poster: UIImage?
    @Published private(set) var isReady = false  // first frame is up; the starting card goes
    @Published var playError: String?

    var api: ServerAPI?
    private var key: String?
    private var lastCommandID: Int {
        get { UserDefaults.standard.integer(forKey: "lastCommandID") }
        set { UserDefaults.standard.set(newValue, forKey: "lastCommandID") }
    }
    private var readyObservation: AnyCancellable?
    private var pollTask: Task<Void, Never>?
    private var statusCount = 0

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

    // MARK: - Choosing a movie in the app

    func play(_ movie: Movie) async {
        var info = MovieInfo(title: movie.title, year: movie.year, summary: movie.summary)
        if let stream = movie.demoStream {
            info.posterImage = movie.poster.flatMap { UIImage(named: $0) }
            start(stream: stream, key: movie.key, at: 0, info: info)
            return
        }
        guard let api else { return }
        info.posterURL = movie.poster.flatMap { api.url($0) }
        do {
            let (data, _) = try await api.request("tv/play", method: "POST", body: ["key": movie.key])
            let details = try JSONDecoder().decode(PlayDetails.self, from: data)
            start(stream: details.url, key: movie.key, at: details.start ?? 0, info: info)
        } catch {
            playError = "Couldn't start \(movie.title): \(error.localizedDescription)"
        }
    }

    // MARK: - marquee links: moviebook://play?url=...&title=...&start=...

    func handle(url: URL) {
        guard url.scheme == "moviebook", url.host == "play",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let stream = items.first(where: { $0.name == "url" })?.value else { return }
        let value = { (name: String) in items.first { $0.name == name }?.value }
        start(stream: stream, key: value("key"), at: value("start").flatMap(Double.init) ?? 0,
              info: MovieInfo(title: value("title"), year: value("year").flatMap(Int.init), summary: value("summary")))
    }

    // MARK: - Remote starts (NFC taps) through the server's inbox

    private func poll() async {
        guard let api else { return }
        do {
            let (data, status) = try await api.request("tv/inbox?after=\(lastCommandID)")
            if status == 200 {
                let command = try JSONDecoder().decode(InboxCommand.self, from: data)
                lastCommandID = command.id
                run(command, api: api)
            }
        } catch {
            return  // the app's screens show connection problems; keep polling quietly
        }
        statusCount += 1
        if statusCount % 3 == 0 { await reportStatus() }
    }

    private func run(_ command: InboxCommand, api: ServerAPI) {
        switch command.action {
        case "play":
            if let stream = command.url {
                start(stream: stream, key: command.key, at: command.start ?? 0,
                      info: MovieInfo(title: command.title, year: command.year, summary: command.summary,
                                      posterURL: command.poster.flatMap { api.url($0) }))
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

    private func start(stream: String, key: String?, at start: Double, info: MovieInfo) {
        guard let url = URL(string: stream) else {
            playError = "That isn't a stream address."
            return
        }
        let item = AVPlayerItem(url: url)
        item.externalMetadata = Self.metadata(for: info, artwork: info.posterImage?.jpegData(compressionQuality: 0.9))
        self.info = info
        poster = info.posterImage
        isReady = false
        if let posterURL = info.posterURL {
            Task {
                guard let (data, _) = try? await URLSession.shared.data(from: posterURL),
                      let image = UIImage(data: data) else { return }
                guard self.player?.currentItem === item else { return }  // still the same movie?
                self.poster = image
                item.externalMetadata = Self.metadata(for: info, artwork: data)
            }
        }
        readyObservation = item.publisher(for: \.status)
            .filter { $0 != .unknown }
            .first()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                if status == .failed {
                    self?.playError = "This movie couldn't be played. \(item.error?.localizedDescription ?? "")"
                    self?.stop()
                } else {
                    self?.isReady = true
                }
            }
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
        self.key = key
        self.player = player
        player.play()
    }

    func stop() {
        player?.pause()
        player = nil
        info = MovieInfo()
        poster = nil
        isReady = false
        readyObservation = nil
        key = nil
        Task { await reportStatus() }
    }

    /// Title, year, summary and poster for Apple's player (the info panel on the Siri Remote).
    private static func metadata(for info: MovieInfo, artwork: Data?) -> [AVMetadataItem] {
        func item(_ identifier: AVMetadataIdentifier, _ value: (NSCopying & NSObjectProtocol)?,
                  dataType: String? = nil) -> AVMetadataItem? {
            guard let value else { return nil }
            let item = AVMutableMetadataItem()
            item.identifier = identifier
            item.value = value
            item.extendedLanguageTag = "und"
            if let dataType { item.dataType = dataType }
            return item.copy() as? AVMetadataItem
        }
        return [
            item(.commonIdentifierTitle, info.title as NSString?),
            item(.iTunesMetadataTrackSubTitle, info.year.map { String($0) as NSString }),
            item(.commonIdentifierDescription, info.summary.flatMap { $0.isEmpty ? nil : $0 as NSString }),
            item(.commonIdentifierArtwork, artwork as NSData?, dataType: kCMMetadataBaseDataType_JPEG as String),
        ].compactMap { $0 }
    }

    private func reportStatus() async {
        guard let api else { return }
        var status: [String: Any] = ["state": "idle"]
        if let player, let item = player.currentItem {
            status["state"] = player.timeControlStatus == .paused ? "paused" : "playing"
            let position = player.currentTime().seconds
            status["position"] = position.isFinite ? Int(position) : 0
            let duration = item.duration.seconds
            status["duration"] = duration.isFinite ? Int(duration) : 0
            status["title"] = info.title ?? ""
            status["key"] = key ?? ""
        }
        _ = try? await api.request("tv/status", method: "POST", body: status)
    }
}
