import AVFoundation
import Combine
import Foundation
import UIKit

/// A command left for this Apple TV by the server (`GET /tv/inbox`), e.g. from an NFC tap.
struct InboxCommand: Decodable {
    let id: Int
    let action: String       // play, pause, resume, stop, seek; audio, audio_pause, audio_resume, audio_stop; nowplaying
    let url: String?         // play: an HLS (or MP4) stream address; audio: the track's
    let title: String?
    let key: String?         // the movie's key, e.g. tmdb-603, reported back in /tv/status
    let start: Double?       // play: seconds to start at
    let by: Double?          // seek: seconds to jump (negative goes back)
    let year: Int?
    let summary: String?
    let poster: String?      // a path on the server
    // Music Marquee plays itself (see NowPlaying.swift)
    let play: String?        // audio: the server's ID for the track, reported back when it ends
    let artist: String?
    let album: String?
    let duration: Double?
}

/// A stream ready to play: what `/tv/play` returns.
private struct PlayDetails: Decodable {
    let url: String
    let start: Double?
    let title: String?
    let year: Int?
    let summary: String?
    let poster: String?  // a path on the server
}

/// Another player that Marquee on an iPhone or iPad can send its movies to, as the server lists it.
struct RemotePlayer: Decodable, Identifiable, Hashable {
    let id: String    // e.g. "app:Den Apple TV"
    let name: String
    let kind: String  // Marquee, Jellyfin, Plex or Home Assistant
}

private struct RemotePlayers: Decodable {
    let players: [RemotePlayer]
    let selected: String?
}

private struct RemoteStart: Decodable { let title: String }

/// A movie's progress, from `/tv/movie/KEY`.
private struct MovieProgress: Decodable {
    let title: String
    let duration: Int  // seconds
    let watched: Int   // seconds
}

/// A scanned movie that's partly watched: resume it, or start over?
struct ResumeOffer: Identifiable {
    let id = UUID()
    let key: String
    let title: String
    let watched: Int  // seconds

    var watchedText: String {
        watched >= 3600 ? String(format: "%d:%02d:%02d", watched / 3600, watched / 60 % 60, watched % 60)
                        : String(format: "%d:%02d", watched / 60, watched % 60)
    }
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
    /// iPhone and iPad: where movies play, nil for right here; the server keeps the choice,
    /// so tags tapped with this device (which open in Safari) follow it too.
    @Published private(set) var playsOn: String?
    @Published private(set) var remotePlayers: [RemotePlayer] = []
    @Published private(set) var notice: String?  // e.g. "Starting Alien on Den"
    @Published var resumeOffer: ResumeOffer?

    /// Music and audiobooks: played here, or on the speakers of this room's NFC reader
    let nowPlaying = NowPlayingModel()

    init() {
        nowPlaying.onStatusChange = { [weak self] in Task { await self?.reportStatus() } }
    }

    var api: ServerAPI? {
        didSet {
            nowPlaying.api = api
            #if os(iOS)
            Task { await refreshPlayers() }
            #endif
        }
    }
    private var key: String?
    private var lastCommandID: Int {
        get { UserDefaults.standard.integer(forKey: "lastCommandID") }
        set { UserDefaults.standard.set(newValue, forKey: "lastCommandID") }
    }
    private var readyObservation: AnyCancellable?
    private var playbackObservations: Set<AnyCancellable> = []
    private var pollTask: Task<Void, Never>?
    private var statusCount = 0

    // MARK: - App lifecycle

    func becameActive() {
        #if os(iOS)
        Task { await refreshPlayers() }  // also how the server learns this device's address, for its taps
        #endif
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.poll()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func resignedActive() {
        // Music Marquee plays itself carries on in the background, and the server still
        // needs to hear when each track ends
        if nowPlaying.playsHere { return }
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: - Choosing a movie in the app

    func play(_ movie: Movie) async {
        if let stream = movie.demoStream {
            var info = MovieInfo(title: movie.title, year: movie.year, summary: movie.summary)
            info.posterImage = movie.poster.flatMap { UIImage(named: $0) }
            start(stream: stream, key: movie.key, at: 0, info: info)
            return
        }
        await play(key: movie.key, title: movie.title)
    }

    /// Play a movie from the server: here, or on the player chosen to play on. `resume` nil
    /// follows the server's Resume setting.
    private func play(key: String, title: String?, resume: Bool? = nil) async {
        guard let api else { return }
        if let playsOn {
            await start(key: key, resume: resume, on: playsOn, api: api)
            return
        }
        var body: [String: Any] = ["key": key]
        if let resume { body["resume"] = resume }
        do {
            let (data, _) = try await api.request("tv/play", method: "POST", body: body)
            let details = try JSONDecoder().decode(PlayDetails.self, from: data)
            start(stream: details.url, key: key, at: details.start ?? 0,
                  info: MovieInfo(title: details.title ?? title, year: details.year, summary: details.summary,
                                  posterURL: details.poster.flatMap { api.url($0) }))
        } catch {
            playError = "Couldn't start \(title ?? "that movie"): \(error.localizedDescription)"
        }
    }

    // MARK: - Book codes (QR codes on the pages, with the same address as the tags)

    /// Play the movie a scanned code is for, e.g. http://marquee.local:8080/m/tmdb-603
    func play(scanned code: String) async {
        guard let key = Self.movieKey(in: code) else {
            playError = "That code isn't a movie from the movie book."
            return
        }
        guard let api else {
            playError = "Connect a Movie Book server in Settings to play movies from the book."
            return
        }
        // Partly watched (past the first minute, not in the closing credits): ask first
        if let progress = try? await api.get("tv/movie/\(key)", as: MovieProgress.self),
           progress.watched >= 60, progress.duration == 0 || progress.watched < progress.duration - 120 {
            resumeOffer = ResumeOffer(key: key, title: progress.title, watched: progress.watched)
            return
        }
        await play(key: key, title: nil, resume: false)
    }

    /// The answer to a resume offer.
    func play(_ offer: ResumeOffer, resume: Bool) async {
        await play(key: offer.key, title: offer.title, resume: resume)
    }

    static func movieKey(in code: String) -> String? {
        guard let url = URL(string: code.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
        let parts = url.pathComponents
        guard let m = parts.firstIndex(of: "m"), m + 1 < parts.count, !parts[m + 1].isEmpty else { return nil }
        return parts[m + 1]
    }

    // MARK: - Playing on another player (iPhone and iPad)

    var playsOnName: String? {
        playsOn.map { id in remotePlayers.first { $0.id.caseInsensitiveCompare(id) == .orderedSame }?.name ?? id }
    }

    func refreshPlayers() async {
        guard let api else {
            remotePlayers = []
            playsOn = nil
            return
        }
        guard let list = try? await api.get("tv/players", as: RemotePlayers.self) else { return }
        remotePlayers = list.players
        playsOn = list.selected
    }

    /// Play on `player` (a player ID), or here with nil.
    func playOn(_ player: String?) async {
        guard let api else { return }
        let previous = playsOn
        playsOn = player  // show it straight away
        do {
            _ = try await api.request("tv/player", method: "POST", body: ["player": player ?? NSNull()])
        } catch {
            playsOn = previous
            playError = "Couldn't change where movies play: \(error.localizedDescription)"
        }
    }

    private func start(key: String, resume: Bool?, on player: String, api: ServerAPI) async {
        let name = playsOnName ?? player
        var body: [String: Any] = ["key": key, "player": player]
        if let resume { body["resume"] = resume }
        do {
            let (data, _) = try await api.request("tv/start", method: "POST", body: body)
            let started = try JSONDecoder().decode(RemoteStart.self, from: data)
            show("Starting \(started.title) on \(name)")
        } catch {
            playError = "Couldn't start it on \(name): \(error.localizedDescription)"
        }
    }

    private func show(_ text: String) {
        notice = text
        Task {
            try? await Task.sleep(for: .seconds(4))
            if notice == text { notice = nil }
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
            // Take everything waiting (a track for here and a Now Playing can come together)
            for _ in 0..<5 {
                let (data, status) = try await api.request("tv/inbox?after=\(lastCommandID)")
                guard status == 200 else { break }
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
        case "audio":
            if player != nil { stop() }  // music chosen for this TV replaces the movie
            nowPlaying.playAudio(command)
        case "audio_pause":
            nowPlaying.pauseAudio()
        case "audio_resume":
            nowPlaying.resumeAudio()
        case "audio_stop":
            nowPlaying.stopAudio()
        case "nowplaying":
            if player == nil { nowPlaying.show() }  // not over a movie
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
        // A tap or scan replaces whatever's playing, including a movie carried on in Picture in Picture
        empty(self.player)
        nowPlaying.movieStarting()
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
        // Tell the server straight away when the movie plays or pauses (Home Assistant's
        // triggers follow it), and finish when it ends
        playbackObservations = [
            player.publisher(for: \.timeControlStatus)
                .map { $0 == .paused }
                .removeDuplicates()
                .dropFirst()
                .receive(on: DispatchQueue.main)
                .sink { [weak self] _ in Task { await self?.reportStatus() } },
            NotificationCenter.default.publisher(for: AVPlayerItem.didPlayToEndTimeNotification, object: item)
                .receive(on: DispatchQueue.main)
                .sink { [weak self] _ in self?.stop() },
        ]
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
        Task { await reportStatus() }
    }

    func stop() {
        empty(player)
        player = nil
        info = MovieInfo()
        poster = nil
        isReady = false
        readyObservation = nil
        playbackObservations = []
        key = nil
        Task { await reportStatus() }
    }

    /// Stop a player for good. Picture in Picture can keep one alive after the app lets go of it.
    private func empty(_ player: AVPlayer?) {
        player?.pause()
        player?.replaceCurrentItem(with: nil)
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
        let audio = nowPlaying.audioStatus
        if let audio { status["audio"] = audio.body }
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
