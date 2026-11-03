import Foundation

/// Runs plexnfc on this Mac: sets up a Python environment for it on first launch,
/// then keeps `python -m plexnfc.server` running while the app is open.
///
/// Everything plexnfc saves (the Plex sign-in, paired Apple TVs and phones) lives
/// in ~/Library/Application Support/Marquee Server, which is also where an optional
/// `.env` file can hold extra settings (Jellyfin, Home Assistant…).
@MainActor
final class ServerController: ObservableObject {
    enum State: Equatable {
        case stopped
        case settingUp(String)
        case running
        case failed(String)
    }

    @Published private(set) var state: State = .stopped
    @Published private(set) var log: [String] = []
    /// The Plex server in use, as the server reports it, e.g. "Plex (https://192-168-0-142…plex.direct:32400)"
    @Published private(set) var plexServer: String?

    @Published var plexURL: String { didSet { save(plexURL, "plexURL") } }  // blank: from the Plex sign-in
    @Published var library: String { didSet { save(library, "library") } }
    @Published var appPlayer: String { didSet { save(appPlayer, "appPlayer") } }
    @Published var port: Int { didSet { UserDefaults.standard.set(port, forKey: "port") } }
    @Published var tagAddress: String { didSet { save(tagAddress, "tagAddress") } }  // blank: marquee.local
    @Published var pythonPath: String { didSet { save(pythonPath, "pythonPath") } }  // blank: find one

    let supportDirectory: URL
    private var logFile: FileHandle?  // server.log in the data folder, for when something goes wrong
    private var process: Process?
    private var stopping = false

    init() {
        let defaults = UserDefaults.standard
        // Early builds saved this placeholder; the server is found from the Plex sign-in now
        let savedURL = defaults.string(forKey: "plexURL") ?? ""
        plexURL = savedURL == "http://localhost:32400" ? "" : savedURL
        library = defaults.string(forKey: "library") ?? "Movies"
        appPlayer = defaults.string(forKey: "appPlayer") ?? ""
        port = defaults.object(forKey: "port") as? Int ?? 8080
        tagAddress = defaults.string(forKey: "tagAddress") ?? ""
        pythonPath = defaults.string(forKey: "pythonPath") ?? ""
        supportDirectory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Marquee Server", isDirectory: true)
    }

    private func save(_ value: String, _ key: String) {
        UserDefaults.standard.set(value.trimmingCharacters(in: .whitespaces), forKey: key)
    }

    /// The name phones reach this Mac by. plexnfc answers for it with whatever
    /// address the Mac has, so tags keep working without a fixed IP.
    static let defaultTagHost = "marquee.local"

    /// The address tags are written with, e.g. http://marquee.local:8080
    var publicBaseURL: String {
        let host = tagAddress.trimmingCharacters(in: .whitespaces)
        return "http://\(host.isEmpty ? Self.defaultTagHost : host):\(port)"
    }

    var panelURL: URL { URL(string: "http://localhost:\(port)/")! }

    var statusText: String {
        switch state {
        case .stopped: "Stopped"
        case .settingUp(let step): step
        case .running: "Running at \(publicBaseURL)"
        case .failed(let message): message
        }
    }

    /// Ask the running server which Plex server it's using (found from the sign-in when none is set).
    func refreshPlexServer() async {
        struct Answer: Decodable { let name: String?; let url: String?; let error: String? }
        guard state == .running,
              let (data, _) = try? await URLSession.shared.data(from: panelURL.appendingPathComponent("plex/server")),
              let answer = try? JSONDecoder().decode(Answer.self, from: data) else {
            plexServer = nil
            return
        }
        if let name = answer.name, let url = answer.url {
            plexServer = "Using \(name) at \(url)"
        } else {
            plexServer = answer.error
        }
    }

    // MARK: - Running the server

    func start() {
        guard process == nil, !isSettingUp else { return }
        Task { await launch() }
    }

    func stop() {
        stopping = true
        process?.terminate()
        process = nil
        state = .stopped
    }

    func restart() {
        stop()
        start()
    }

    private var isSettingUp: Bool {
        if case .settingUp = state { return true }
        return false
    }

    private func launch() async {
        stopping = false
        do {
            try FileManager.default.createDirectory(at: supportDirectory, withIntermediateDirectories: true)
            let python = try await preparePython()
            state = .settingUp("Starting…")
            try run(python)
            state = .running
        } catch {
            state = .failed(error.localizedDescription)
            append("⚠︎ \(error.localizedDescription)")
        }
    }

    private func run(_ python: URL) throws {
        let server = Bundle.main.resourceURL!.appendingPathComponent("server")
        let process = Process()
        process.executableURL = python
        process.arguments = ["-c", Self.serverBootstrap, String(port)]
        process.currentDirectoryURL = supportDirectory
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = server.path
        environment["PYTHONUNBUFFERED"] = "1"
        if !plexURL.isEmpty { environment["PLEX_URL"] = plexURL }
        environment["PLEX_LIBRARY"] = library
        environment["PUBLIC_BASE_URL"] = publicBaseURL
        environment["SERVER_NAME"] = "Movie Book on \(Host.current().localizedName ?? "this Mac")"
        if !appPlayer.isEmpty { environment["APP_PLAYER"] = appPlayer }
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.append(text) }
        }
        process.terminationHandler = { [weak self] finished in
            let code = finished.terminationStatus
            Task { @MainActor in self?.serverExited(finished, code: code) }
        }
        let logURL = supportDirectory.appendingPathComponent("server.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)  // one run per file
        logFile = try? FileHandle(forWritingTo: logURL)
        try process.run()
        self.process = process
    }

    /// Runs plexnfc.server, and ends it when this app goes away, however it went
    /// (quitting runs stop(), but a crash or a kill doesn't), so it never holds the port.
    private static let serverBootstrap = """
        import os, runpy, sys, threading, time
        parent = os.getppid()
        def watch():
            while os.getppid() == parent:
                time.sleep(2)
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
        sys.argv = ["plexnfc.server", "--port", sys.argv[1]]
        runpy.run_module("plexnfc.server", run_name="__main__")
        """

    private func serverExited(_ finished: Process, code: Int32) {
        guard finished === process || process == nil else { return }
        process = nil
        if !stopping {
            state = .failed("The server stopped (exit code \(code)). See the log.")
        }
    }

    private func append(_ text: String) {
        logFile?.write(Data(text.utf8))
        log.append(contentsOf: text.split(separator: "\n", omittingEmptySubsequences: true).map(String.init))
        if log.count > 1000 { log.removeFirst(log.count - 1000) }
    }

    // MARK: - Python

    /// The environment's Python, made (or brought up to date) from the bundled requirements.
    private func preparePython() async throws -> URL {
        let venv = supportDirectory.appendingPathComponent("venv", isDirectory: true)
        let python = venv.appendingPathComponent("bin/python3")
        let requirements = Bundle.main.resourceURL!.appendingPathComponent("server/requirements.txt")
        let wanted = (try String(contentsOf: requirements, encoding: .utf8)) + "\n" + Self.appleTVPackages.joined(separator: "\n")
        let installed = venv.appendingPathComponent("marquee-requirements.txt")
        if FileManager.default.isExecutableFile(atPath: python.path),
           (try? String(contentsOf: installed, encoding: .utf8)) == wanted {
            return python
        }
        if !FileManager.default.isExecutableFile(atPath: python.path) {
            state = .settingUp("Finding Python…")
            let base = try await findPython()
            append("Using \(base.path)")
            state = .settingUp("Making a Python environment…")
            try await tool(base, ["-m", "venv", venv.path])
        }
        state = .settingUp("Installing plexnfc's packages (first launch only)…")
        try await tool(python, ["-m", "pip", "install", "--disable-pip-version-check", "-q",
                                "-r", requirements.path] + Self.appleTVPackages)
        try wanted.write(to: installed, atomically: true, encoding: .utf8)
        return python
    }

    /// pyatv, for opening Marquee on Apple TVs. srptools 2 changed a call pyatv 0.18 still
    /// makes the old way, which breaks pairing.
    static let appleTVPackages = ["pyatv>=0.16", "srptools<2"]

    /// A Python 3.10 or newer. Newer than the packages' wheels can fail to install, so
    /// the most recent well-supported versions come first.
    private func findPython() async throws -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var candidates = pythonPath.isEmpty ? [] : [pythonPath]
        for version in ["3.13", "3.12", "3.11", "3.10"] {
            candidates += ["/opt/homebrew/bin/python\(version)", "/usr/local/bin/python\(version)",
                           "/Library/Frameworks/Python.framework/Versions/\(version)/bin/python3"]
        }
        candidates += ["\(home)/.pyenv/shims/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                       "/usr/bin/python3"]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            let url = URL(fileURLWithPath: path)
            if (try? await tool(url, ["-c", "import sys; sys.exit(sys.version_info < (3, 10))"], quiet: true)) != nil {
                return url
            }
        }
        throw ServerError("Install Python 3.10 or newer (for example with Homebrew: brew install python@3.13), "
                          + "or choose one in Settings.")
    }

    /// Run a setup step to the end, with its output in the log.
    @discardableResult
    private func tool(_ executable: URL, _ arguments: [String], quiet: Bool = false) async throws -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = supportDirectory
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        let text: String = try await withCheckedThrowingContinuation { continuation in
            process.terminationHandler = { finished in
                let data = output.fileHandleForReading.readDataToEndOfFile()
                let text = String(data: data, encoding: .utf8) ?? ""
                if finished.terminationStatus == 0 {
                    continuation.resume(returning: text)
                } else {
                    continuation.resume(throwing: ServerError(
                        "\(executable.lastPathComponent) \(arguments.first ?? "") failed (exit code "
                        + "\(finished.terminationStatus)). See the log."))
                }
            }
            do { try process.run() } catch { continuation.resume(throwing: error) }
        }
        if !quiet { append(text) }
        return text
    }
}

struct ServerError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
