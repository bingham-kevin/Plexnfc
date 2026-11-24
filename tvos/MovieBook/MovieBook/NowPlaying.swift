import AVFoundation
import Combine
import MediaPlayer
import SwiftUI

/// Music and audiobooks in Marquee: what the server's `/tv/nowplaying` says is playing, either
/// here (Marquee as a speaker, playing each track the server sends) or on the speakers of the
/// NFC reader whose card was just read in this room.
struct NowPlaying: Decodable, Equatable {
    let state: String               // playing, paused or idle
    var title: String?
    var artist: String?
    var album: String?
    var image: String?              // the cover: an address, or a path on the server
    var position: Double?           // seconds into the track
    var duration: Double?
    var zone: String?
    var speakers: [String]?
    var here: Bool?                 // Marquee plays it itself
    var reader: String?             // the reader whose music this is
    var track: String?              // which track the lyrics are for
    var queue: [QueueItem]?
    var index: Int?
    var book: Book?
    var lyrics: Lyrics?
    var lyrics_loading: Bool?

    struct QueueItem: Decodable, Equatable {
        let name: String?
        let artist: String?
        let duration: Double?
    }

    struct Book: Decodable, Equatable {
        let title: String
        let author: String
        let image: String?
        let chapters: [Chapter]
        let position: Double?       // seconds into the book
        let duration: Double?
    }

    struct Chapter: Decodable, Equatable {
        let title: String
        let start: Double
        let end: Double
    }

    struct Lyrics: Decodable, Equatable {
        let synced: Bool
        let lines: [Line]
    }

    struct Line: Decodable, Equatable {
        let time: Double?
        let text: String
    }

    var isPlaying: Bool { state == "playing" }
    var isIdle: Bool { state == "idle" }
}

/// What this Marquee plays itself, as `/tv/status` reports it (`audio`).
struct AudioStatus: Sendable {
    let play: String
    let state: String
    let ended: String?
    let position: Int

    var body: [String: Any] {
        var body: [String: Any] = ["play": play, "state": state, "position": position]
        if let ended { body["ended"] = ended }
        return body
    }
}

/// Playback of music sent to this Marquee, and the Now Playing screen.
@MainActor
final class NowPlayingModel: ObservableObject {
    @Published private(set) var now: NowPlaying?
    @Published private(set) var fetchedAt = Date()
    @Published var visible = false
    @Published private(set) var cover: UIImage?
    @Published var error: String?

    var api: ServerAPI? {
        didSet { if api == nil { stopAudio(); visible = false; now = nil } }
    }
    /// Tell the server what's playing (PlayerModel's status report)
    var onStatusChange: () -> Void = {}

    // Music this Marquee plays itself
    private var audio: AVPlayer?
    private var token: String?           // the server's ID for the track playing here
    private var endedToken: String?      // the last track that played to its end
    private var audioState = "idle"      // playing, paused, ended, stopped
    private var audioInfo: (title: String, artist: String, album: String, duration: Double?) = ("", "", "", nil)
    private var observations: Set<AnyCancellable> = []
    private var pollTask: Task<Void, Never>?
    private var coverAddress: String?
    private var remoteCommandsReady = false

    var playsHere: Bool { audio != nil && audioState != "stopped" }
    /// Something to go back to from the browsing screens
    var hasSomething: Bool { playsHere || (now.map { !$0.isIdle } ?? false) }

    // MARK: - Commands from the server's inbox

    func playAudio(_ command: InboxCommand) {
        guard let address = command.url, let url = URL(string: address) else { return }
        audio?.pause()
        let item = AVPlayerItem(url: url)
        let player = AVPlayer(playerItem: item)
        player.automaticallyWaitsToMinimizeStalling = true
        token = command.play
        audioState = "playing"
        audioInfo = (command.title ?? "", command.artist ?? "", command.album ?? "", command.duration)
        observations = [
            NotificationCenter.default.publisher(for: AVPlayerItem.didPlayToEndTimeNotification, object: item)
                .receive(on: DispatchQueue.main)
                .sink { [weak self] _ in self?.trackEnded() },
            item.publisher(for: \.status)
                .filter { $0 == .failed }
                .receive(on: DispatchQueue.main)
                .sink { [weak self] _ in
                    self?.error = "Couldn't play \(command.title ?? "that track"). \(item.error?.localizedDescription ?? "")"
                    self?.audioState = "stopped"
                    self?.onStatusChange()
                },
            player.publisher(for: \.timeControlStatus)
                .removeDuplicates()
                .receive(on: DispatchQueue.main)
                .sink { [weak self] _ in self?.updateSystemNowPlaying() },
        ]
        if let start = command.start, start > 0 {
            player.seek(to: CMTime(seconds: start, preferredTimescale: 600))
        }
        audio = player
        setUpAudioSession()
        player.play()
        visible = true
        updateSystemNowPlaying()
        startPolling()
        onStatusChange()
        Task { await refresh() }
    }

    func pauseAudio() {
        audio?.pause()
        if audioState == "playing" { audioState = "paused" }
        onStatusChange()
        Task { await refresh() }
    }

    func resumeAudio() {
        audio?.play()
        if audioState == "paused" { audioState = "playing" }
        onStatusChange()
        Task { await refresh() }
    }

    /// The server stopped the music (or another movie or track replaced it).
    func stopAudio() {
        audio?.pause()
        audio?.replaceCurrentItem(with: nil)
        audio = nil
        observations = []
        if audioState != "idle" { audioState = "stopped" }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        onStatusChange()
    }

    /// A movie is starting here: the music stops (the server hears, and stops its queue).
    func movieStarting() {
        guard playsHere else {
            visible = false
            return
        }
        stopAudio()
        visible = false
    }

    /// A card was read on this room's reader: show what its speakers play.
    func show() {
        visible = true
        startPolling()
        Task { await refresh() }
    }

    private func trackEnded() {
        endedToken = token
        audioState = "ended"
        onStatusChange()
    }

    /// For `/tv/status`: what this Marquee plays itself.
    var audioStatus: AudioStatus? {
        guard let token else { return nil }
        let seconds = audio?.currentTime().seconds ?? 0
        return AudioStatus(play: token, state: audioState, ended: endedToken,
                           position: seconds.isFinite ? Int(seconds) : 0)
    }

    // MARK: - The server's Now Playing

    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.visible || self.playsHere else { break }
                await self.refresh()
                try? await Task.sleep(for: .seconds(1.5))
            }
            self?.pollTask = nil
        }
    }

    func refresh() async {
        guard let api else { return }
        guard let fetched = try? await api.get("tv/nowplaying", as: NowPlaying.self) else { return }
        now = fetched
        fetchedAt = Date()
        await loadCover(fetched.image, api: api)
        updateSystemNowPlaying()
    }

    private func loadCover(_ address: String?, api: ServerAPI) async {
        guard address != coverAddress else { return }
        coverAddress = address
        guard let address, let url = address.hasPrefix("http") ? URL(string: address) : api.url(address) else {
            cover = nil
            return
        }
        if let (data, _) = try? await URLSession.shared.data(from: url), coverAddress == address {
            cover = UIImage(data: data)
            updateSystemNowPlaying()
        }
    }

    /// Where the track is now: here, from the player; else counted on from the server's last answer.
    func position(at date: Date) -> Double {
        if let now, now.here == true, let audio, audioState != "stopped" {
            let seconds = audio.currentTime().seconds
            if seconds.isFinite { return seconds }
        }
        guard let now else { return 0 }
        var position = now.position ?? 0
        if now.isPlaying { position += date.timeIntervalSince(fetchedAt) }
        if let duration = now.duration, duration > 0 { position = min(position, duration) }
        return max(0, position)
    }

    // MARK: - Controls

    func send(_ action: String, value: Double? = nil) {
        guard let api else { return }
        // Show a pause or play straight away
        if action == "toggle", var shown = now {
            shown = NowPlaying(state: shown.isPlaying ? "paused" : "playing", title: shown.title, artist: shown.artist,
                               album: shown.album, image: shown.image, position: position(at: Date()),
                               duration: shown.duration, zone: shown.zone, speakers: shown.speakers, here: shown.here,
                               reader: shown.reader, track: shown.track, queue: shown.queue, index: shown.index,
                               book: shown.book, lyrics: shown.lyrics, lyrics_loading: shown.lyrics_loading)
            now = shown
            fetchedAt = Date()
        }
        Task {
            var body: [String: Any] = ["action": action]
            if let value { body["value"] = value }
            do {
                let (data, _) = try await api.request("tv/nowplaying", method: "POST", body: body)
                if action != "hide", let fetched = try? JSONDecoder().decode(NowPlaying.self, from: data) {
                    now = fetched
                    fetchedAt = Date()
                }
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    /// Menu on the remote: back to browsing. A reader's music stops showing here; music Marquee
    /// plays itself carries on, and the Now Playing button brings it back.
    func hide() {
        visible = false
        if now?.here != true { send("hide") }
    }

    // MARK: - The system's Now Playing (Control Center, the Siri Remote's buttons)

    private func setUpAudioSession() {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
        try? AVAudioSession.sharedInstance().setActive(true)
        guard !remoteCommandsReady else { return }
        remoteCommandsReady = true
        let center = MPRemoteCommandCenter.shared()
        let handlers: [(MPRemoteCommand, String)] = [
            (center.togglePlayPauseCommand, "toggle"), (center.playCommand, "play"), (center.pauseCommand, "pause"),
            (center.nextTrackCommand, "next"), (center.previousTrackCommand, "previous"),
        ]
        for (command, action) in handlers {
            Self.handle(command, action, model: self)
        }
    }

    /// The system calls these on a queue of its own: hop to the main actor.
    private nonisolated static func handle(_ command: MPRemoteCommand, _ action: String, model: NowPlayingModel) {
        command.addTarget { [weak model] _ in
            Task { @MainActor in model?.send(action) }
            return .success
        }
    }

    private func updateSystemNowPlaying() {
        guard playsHere, let audio else { return }
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: now?.title ?? audioInfo.title,
            MPMediaItemPropertyArtist: now?.artist ?? audioInfo.artist,
            MPMediaItemPropertyAlbumTitle: now?.album ?? audioInfo.album,
            MPNowPlayingInfoPropertyPlaybackRate: audio.timeControlStatus == .paused ? 0.0 : 1.0,
        ]
        let elapsed = audio.currentTime().seconds
        if elapsed.isFinite { info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = elapsed }
        if let duration = now?.duration ?? audioInfo.duration { info[MPMediaItemPropertyPlaybackDuration] = duration }
        if let cover {
            info[MPMediaItemPropertyArtwork] = Self.artwork(cover)
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    /// The system asks for the picture on a queue of its own, so the closure mustn't belong to the main actor.
    private nonisolated static func artwork(_ image: UIImage) -> MPMediaItemArtwork {
        MPMediaItemArtwork(boundsSize: image.size) { _ in image }
    }
}

// MARK: - The screen

/// Sizes for across the room, or in the hand.
private enum NowMetrics {
    #if os(tvOS)
    static let cover: CGFloat = 560
    static let margin: CGFloat = 90
    static let spacing: CGFloat = 70
    static let title = Font.system(size: 56, weight: .bold)
    static let subtitle = Font.title2
    static let lyric = Font.system(size: 40, weight: .semibold)
    static let button: CGFloat = 44
    #else
    static let cover: CGFloat = 280
    static let margin: CGFloat = 20
    static let spacing: CGFloat = 24
    static let title = Font.title.bold()
    static let subtitle = Font.headline
    static let lyric = Font.title3.weight(.semibold)
    static let button: CGFloat = 28
    #endif
}

struct NowPlayingView: View {
    @EnvironmentObject private var model: NowPlayingModel

    var body: some View {
        ZStack {
            background.ignoresSafeArea()
            if let now = model.now, !now.isIdle {
                content(now)
            } else {
                VStack(spacing: 24) {
                    ProgressView()
                    Text(model.now == nil ? "Loading…" : "Nothing is playing").font(.title3).foregroundStyle(.secondary)
                }
            }
            #if os(iOS)
            VStack {
                HStack {
                    Button("Done") { model.hide() }.fontWeight(.semibold)
                    Spacer()
                }
                .padding()
                Spacer()
            }
            #endif
        }
        #if os(tvOS)
        .ignoresSafeArea()
        #endif
        .environment(\.colorScheme, .dark)
        #if os(tvOS)
        .onExitCommand { model.hide() }
        .onPlayPauseCommand { model.send("toggle") }
        #endif
        .onAppear { model.startPolling() }
    }

    /// The cover, blurred, behind it all. Overlays, so the picture can't make the screen any wider.
    private var background: some View {
        Color.black
            .overlay {
                if let cover = model.cover {
                    Image(uiImage: cover).resizable().scaledToFill().blur(radius: 80).opacity(0.5)
                }
            }
            .overlay(LinearGradient(colors: [.black.opacity(0.2), .black.opacity(0.7)], startPoint: .top, endPoint: .bottom))
            .clipped()
    }

    @ViewBuilder private func content(_ now: NowPlaying) -> some View {
        let layout = isWide ? AnyLayout(HStackLayout(alignment: .center, spacing: NowMetrics.spacing))
                            : AnyLayout(VStackLayout(spacing: NowMetrics.spacing))
        layout {
            VStack(alignment: .leading, spacing: 28) {
                coverView
                details(now)
                TimelineView(.periodic(from: .now, by: 0.5)) { context in
                    Timeline(now: now, position: model.position(at: context.date))
                }
                Controls(now: now)
            }
            .frame(maxWidth: isWide ? NowMetrics.cover + 220 : .infinity, alignment: .leading)

            SidePanel(now: now)
                .frame(maxWidth: .infinity, maxHeight: isWide ? 860 : 360)
        }
        .padding(NowMetrics.margin)
    }

    private var isWide: Bool {
        #if os(tvOS)
        true
        #else
        UIDevice.current.userInterfaceIdiom == .pad
        #endif
    }

    private var coverView: some View {
        Group {
            if let cover = model.cover {
                Image(uiImage: cover).resizable().scaledToFill()
            } else {
                RoundedRectangle(cornerRadius: 24).fill(.white.opacity(0.08))
                    .overlay(Image(systemName: model.now?.book != nil ? "book.closed" : "music.note")
                        .font(.system(size: NowMetrics.cover / 4)).foregroundStyle(.secondary))
            }
        }
        .frame(width: NowMetrics.cover, height: NowMetrics.cover)
        .clipShape(RoundedRectangle(cornerRadius: NowMetrics.cover / 24))
        .shadow(radius: 40)
    }

    @ViewBuilder private func details(_ now: NowPlaying) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let book = now.book {
                Text(book.title).font(NowMetrics.title).lineLimit(2)
                Text(book.author).font(NowMetrics.subtitle).foregroundStyle(.secondary)
            } else {
                Text(now.title ?? "").font(NowMetrics.title).lineLimit(2)
                Text([now.artist, now.album].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · "))
                    .font(NowMetrics.subtitle).foregroundStyle(.secondary).lineLimit(1)
            }
            if let speakers = now.speakers, !speakers.isEmpty, now.here != true {
                Label(speakers.joined(separator: " + "), systemImage: "hifispeaker")
                    .font(.callout).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

/// The timeline: the track's, or for a book, the chapter's, with how much of the book is left.
private struct Timeline: View {
    let now: NowPlaying
    let position: Double

    var body: some View {
        let (elapsed, total, label) = values
        VStack(alignment: .leading, spacing: 10) {
            ProgressView(value: total > 0 ? min(elapsed, total) : 0, total: max(total, 1))
                .tint(.white)
            HStack {
                Text(time(elapsed))
                Spacer()
                if let label { Text(label).lineLimit(1) }
                Spacer()
                Text("-" + time(max(0, total - elapsed)))
            }
            .font(.callout.monospacedDigit())
            .foregroundStyle(.secondary)
        }
    }

    /// Elapsed and total seconds, and what they're of
    private var values: (Double, Double, String?) {
        guard let book = now.book, let bookPosition = bookPosition else {
            return (position, now.duration ?? 0, nil)
        }
        let left = max(0, (book.duration ?? 0) - bookPosition)
        if let index = book.chapters.lastIndex(where: { $0.start <= bookPosition }) {
            let chapter = book.chapters[index]
            return (bookPosition - chapter.start, chapter.end - chapter.start,
                    "\(chapter.title) · \(index + 1) of \(book.chapters.count) · \(time(left)) left")
        }
        return (bookPosition, book.duration ?? 0, "\(time(left)) left")
    }

    /// Where in the book: the server's, counted on as the track plays
    private var bookPosition: Double? {
        guard let book = now.book, let at = book.position else { return nil }
        return at + max(0, position - (now.position ?? 0))
    }

    private func time(_ seconds: Double) -> String {
        let t = Int(seconds.rounded())
        return t >= 3600 ? String(format: "%d:%02d:%02d", t / 3600, t / 60 % 60, t % 60)
                         : String(format: "%d:%02d", t / 60, t % 60)
    }
}

private struct Controls: View {
    @EnvironmentObject private var model: NowPlayingModel
    let now: NowPlaying

    var body: some View {
        HStack(spacing: 40) {
            if let book = now.book {
                button("backward.end.fill", "Previous chapter") { chapter(book, by: -1) }
                button("gobackward.30", "Back 30 seconds") { seek(by: -30) }
                playPause
                button("goforward.30", "Forward 30 seconds") { seek(by: 30) }
                button("forward.end.fill", "Next chapter") { chapter(book, by: 1) }
            } else {
                button("backward.fill", "Previous") { model.send("previous") }
                playPause
                button("forward.fill", "Next") { model.send("next") }
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var playPause: some View {
        button(now.isPlaying ? "pause.fill" : "play.fill", now.isPlaying ? "Pause" : "Play") { model.send("toggle") }
    }

    private func button(_ symbol: String, _ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: NowMetrics.button)).frame(width: NowMetrics.button * 1.6,
                                                                                    height: NowMetrics.button * 1.6)
        }
        #if os(tvOS)
        .buttonStyle(.card)
        #else
        .buttonStyle(.plain)
        #endif
        .accessibilityLabel(label)
    }

    private func seek(by seconds: Double) {
        model.send("seek", value: max(0, model.position(at: Date()) + seconds))
    }

    private func chapter(_ book: NowPlaying.Book, by step: Int) {
        guard let at = book.position else { return }
        let here = at + max(0, model.position(at: Date()) - (now.position ?? 0))
        let current = book.chapters.lastIndex(where: { $0.start <= here }) ?? 0
        // Back goes to the start of this chapter unless it's only just begun
        var target = current + step
        if step < 0, here - (book.chapters[safe: current]?.start ?? 0) > 5 { target = current }
        guard let chapter = book.chapters[safe: target] else { return }
        model.send("book", value: chapter.start)
    }
}

/// Lyrics, if there are some; else what's up next (or a book's chapters).
private struct SidePanel: View {
    @EnvironmentObject private var model: NowPlayingModel
    let now: NowPlaying

    var body: some View {
        if let lyrics = now.lyrics, !lyrics.lines.isEmpty {
            LyricsView(lyrics: lyrics)
        } else if let queue = now.queue, queue.count > 1, now.book == nil {
            UpNext(queue: queue, index: now.index ?? 0)
        } else if now.lyrics_loading == true {
            ProgressView()
        } else {
            Color.clear
        }
    }
}

private struct LyricsView: View {
    @EnvironmentObject private var model: NowPlayingModel
    let lyrics: NowPlaying.Lyrics

    var body: some View {
        if lyrics.synced {
            TimelineView(.periodic(from: .now, by: 0.25)) { context in
                let current = currentLine(at: model.position(at: context.date))
                ScrollViewReader { reader in
                    ScrollView(.vertical) {
                        VStack(alignment: .leading, spacing: 28) {
                            ForEach(Array(lyrics.lines.enumerated()), id: \.offset) { index, line in
                                Text(line.text.isEmpty ? "♪" : line.text)
                                    .font(NowMetrics.lyric)
                                    .foregroundStyle(index == current ? .white : .white.opacity(0.35))
                                    .id(index)
                            }
                        }
                        .padding(.vertical, 300)
                    }
                    .scrollIndicators(.hidden)
                    .onChange(of: current) { _, line in
                        withAnimation(.easeInOut(duration: 0.4)) { reader.scrollTo(line, anchor: .center) }
                    }
                    .onAppear { reader.scrollTo(current, anchor: .center) }
                }
            }
            .mask(LinearGradient(colors: [.clear, .black, .black, .clear], startPoint: .top, endPoint: .bottom))
        } else {
            ScrollView(.vertical) {
                Text(lyrics.lines.map(\.text).joined(separator: "\n"))
                    .font(NowMetrics.lyric.weight(.regular))
                    .foregroundStyle(.white.opacity(0.8))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            #if os(tvOS)
            .focusable()  // so the remote can scroll plain lyrics
            #endif
        }
    }

    private func currentLine(at position: Double) -> Int {
        lyrics.lines.lastIndex { ($0.time ?? 0) <= position + 0.2 } ?? 0
    }
}

private struct UpNext: View {
    @EnvironmentObject private var model: NowPlayingModel
    let queue: [NowPlaying.QueueItem]
    let index: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Tracks").font(.title3.bold()).foregroundStyle(.secondary)
            ScrollViewReader { reader in
                ScrollView(.vertical) {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(queue.enumerated()), id: \.offset) { n, item in
                            Button { model.send("jump", value: Double(n)) } label: {
                                HStack(spacing: 20) {
                                    Text("\(n + 1)").monospacedDigit().foregroundStyle(.secondary).frame(width: 50, alignment: .trailing)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(item.name ?? "").fontWeight(n == index ? .bold : .regular).lineLimit(1)
                                        if let artist = item.artist, !artist.isEmpty {
                                            Text(artist).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                        }
                                    }
                                    Spacer()
                                    if n == index {
                                        Image(systemName: "speaker.wave.2.fill")
                                    } else if let duration = item.duration {
                                        Text(String(format: "%d:%02d", Int(duration) / 60, Int(duration) % 60))
                                            .monospacedDigit().foregroundStyle(.secondary)
                                    }
                                }
                                .padding(.vertical, 8)
                                .opacity(n < index ? 0.5 : 1)
                            }
                            #if os(tvOS)
                            .buttonStyle(.plain)
                            #else
                            .buttonStyle(.plain)
                            #endif
                            .id(n)
                        }
                    }
                }
                .onAppear { reader.scrollTo(index, anchor: .top) }
                .onChange(of: index) { _, now in withAnimation { reader.scrollTo(now, anchor: .top) } }
            }
        }
    }
}

/// The way back to Now Playing from the browsing screens.
struct NowPlayingButton: View {
    @EnvironmentObject private var model: NowPlayingModel

    var body: some View {
        Button { model.show() } label: {
            Label(model.now?.book?.title ?? model.now?.title ?? "Now Playing", systemImage: "waveform")
                .lineLimit(1)
                #if os(iOS)
                .font(.callout.weight(.semibold))
                .padding(.horizontal, 18).padding(.vertical, 12)
                .background(.regularMaterial, in: Capsule())
                .shadow(radius: 8, y: 2)
                #endif
        }
        #if os(iOS)
        .buttonStyle(.plain)
        #endif
    }
}

private extension Array {
    subscript(safe index: Int) -> Element? { indices.contains(index) ? self[index] : nil }
}
