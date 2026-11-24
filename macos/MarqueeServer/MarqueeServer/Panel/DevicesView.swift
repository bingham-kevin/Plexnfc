import SwiftUI

/// Devices waiting for their code to be approved. Shown on every tab while any are waiting.
struct PairingRequestsView: View {
    let model: PanelModel
    let overview: Overview

    var body: some View {
        if !overview.pairingRequests.isEmpty {
            PanelSection("Waiting to pair", subtitle: "Check the code matches the one on the device, name it (Marquee "
                         + "players after their room or owner), then approve.", attention: true) {
                ForEach(overview.pairingRequests) { request in
                    RequestRow(model: model, request: request, canManage: overview.canManage)
                }
            }
        }
    }
}

private struct RequestRow: View {
    let model: PanelModel
    let request: PairingRequest
    let canManage: Bool
    @State private var name = ""
    @State private var busy = false

    var body: some View {
        HStack(spacing: 12) {
            Text(request.code).font(.system(.title2, design: .monospaced).weight(.bold)).textSelection(.enabled)
            KindTag(text: request.kind.label)
            TextField("Name", text: $name).frame(maxWidth: 240).disabled(!canManage)
                .onSubmit(approve)
            if canManage {
                Button("Approve", action: approve).buttonStyle(.borderedProminent).disabled(busy)
            }
        }
        .onAppear { if name.isEmpty { name = request.name } }
    }

    private func approve() {
        busy = true
        Task {
            await model.perform("Paired \(name.isEmpty ? request.name : name)") {
                try await model.api.post("pairings/\(request.pairId)/approve", ["name": name])
            }
            busy = false
        }
    }
}

/// Everything paired with this server.
struct DevicesView: View {
    let model: PanelModel
    let overview: Overview
    @State private var players: [Player] = []

    var body: some View {
        let order = DeviceKind.allCases
        let devices = overview.devices.sorted {
            (order.firstIndex(of: $0.kind)!, $0.name.localizedLowercase) < (order.firstIndex(of: $1.kind)!, $1.name.localizedLowercase)
        }
        PanelSection("Paired", subtitle: "In Marquee (Apple TV, iPhone, iPad), Marquee Remote, a Movie Remote box or "
                     + "Home Assistant's Marquee integration, connect to \(overview.tagAddress.components(separatedBy: "://").last ?? "") "
                     + "and approve the code it shows. Tags pick a Marquee player with ?client=app:NAME.") {
            if devices.isEmpty {
                Text("Nothing paired yet.").foregroundStyle(.secondary)
            } else {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 8) {
                    GridRow {
                        Text("Name"); Text("App"); Text("Status"); Text("Paired"); Text("")
                    }
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    ForEach(devices) { device in
                        Divider()
                        DeviceRow(model: model, device: device, canManage: overview.canManage, players: players)
                    }
                }
            }
        }
        .task {
            // Only for choosing a Movie Remote's player: the paired Marquee Apple TVs
            if overview.devices.contains(where: { $0.kind == .remote }),
               let found: Players = try? await model.api.get("players") {
                players = found.players
            }
        }
    }
}

private struct DeviceRow: View {
    let model: PanelModel
    let device: Device
    let canManage: Bool
    let players: [Player]
    @State private var name = ""

    var body: some View {
        GridRow(alignment: .center) {
            if canManage {
                HStack {
                    TextField("Name", text: $name).frame(width: 180).onSubmit(rename)
                    if changed { Button("Save", action: rename) }
                }
            } else {
                Text(device.name)
            }
            KindTag(text: device.kind.label)
            HStack {
                Text(describe).foregroundStyle(.secondary).lineLimit(1)
                if device.kind == .remote { playerChoice }
            }
            Text(Date(timeIntervalSince1970: device.paired), format: .dateTime.day().month().year())
                .foregroundStyle(.secondary)
            if canManage {
                Button("Remove", role: .destructive) {
                    guard confirm("Remove \(device.name)?", detail: "It will need pairing again.", button: "Remove") else { return }
                    Task { await model.perform("Removed \(device.name)") { try await model.api.post("devices/\(device.id)/remove") } }
                }
            }
        }
        .onAppear { name = device.name }
        .onChange(of: device.name) { _, new in name = new }
    }

    private var changed: Bool {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        return !trimmed.isEmpty && trimmed != device.name
    }

    private var describe: String {
        switch device.kind {
        case .phone: "Taps go to \(device.player ?? "the default player")"
        case .homeassistant: "Media players and triggers in Home Assistant"
        case .remote: "Tags and buttons go to"
        case .tv:
            if let status = device.status {
                "\(status.state == "paused" ? "Paused" : "Playing"): \(status.title ?? "")"
            } else {
                device.player.map { "Plays on \($0)" } ?? "Marquee player"
            }
        }
    }

    private func rename() {
        guard changed else { return }
        let done = device.kind == .tv
            ? "Renamed. If a setting or tag names app:\(device.name), change it to the new name too." : "Renamed"
        Task { await model.perform(done) { try await model.api.post("devices/\(device.id)/rename", ["name": name]) } }
    }

    /// Which player a Movie Remote controls.
    @ViewBuilder private var playerChoice: some View {
        let current = device.player ?? ""
        if canManage {
            Picker("Player", selection: Binding(get: { current }, set: { choose($0) })) {
                Text("The default player").tag("")
                if !current.isEmpty, !players.contains(where: { $0.id == current }) { Text(current).tag(current) }
                ForEach(players) { Text($0.name).tag($0.id) }
            }
            .labelsHidden()
            .frame(maxWidth: 240)
        } else {
            Text(players.first { $0.id == current }?.name ?? (current.isEmpty ? "the default player" : current))
        }
    }

    private func choose(_ player: String) {
        Task {
            await model.perform("\(device.name) will play there") {
                try await model.api.post("devices/\(device.id)/player", ["player": player.isEmpty ? NSNull() : player])
            }
        }
    }
}

/// Apple TVs this server switches on and opens Marquee on itself, with no Home Assistant script.
struct AppleTVsView: View {
    let model: PanelModel
    let overview: Overview
    @State private var found: [FoundAppleTV]?
    @State private var looking = false

    var body: some View {
        if overview.appleTvs.available {
            PanelSection("Open Marquee automatically", subtitle: "Pair an Apple TV here and a tap switches it on and opens Marquee.") {
                if !overview.appleTvs.linked.isEmpty {
                    Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 8) {
                        GridRow {
                            Text("Marquee player"); Text("Apple TV"); Text("")
                        }
                        .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                        ForEach(overview.appleTvs.linked) { tv in
                            Divider()
                            LinkedRow(model: model, tv: tv, canManage: overview.canManage)
                        }
                    }
                }
                if overview.canManage {
                    Button(looking ? "Looking…" : "Find Apple TVs") { scan() }
                        .buttonStyle(.borderedProminent)
                        .disabled(looking)
                    if let found {
                        if found.isEmpty {
                            Text("No Apple TVs found. Are they on, and on this network?").foregroundStyle(.secondary)
                        } else {
                            ForEach(found) { tv in
                                FoundRow(model: model, tv: tv,
                                         marquees: overview.devices.filter { $0.kind == .tv }.map(\.name)) {
                                    self.found = nil
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func scan() {
        looking = true
        Task {
            struct Answer: Decodable { let found: [FoundAppleTV] }
            do {
                let answer: Answer = try await model.api.post("appletvs/scan")
                found = answer.found
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            looking = false
        }
    }
}

private struct LinkedRow: View {
    let model: PanelModel
    let tv: LinkedAppleTV
    let canManage: Bool
    @State private var busy = false

    var body: some View {
        GridRow {
            Text(tv.player)
            HStack { Text(tv.name); Text(tv.address).foregroundStyle(.secondary) }
            if canManage {
                HStack {
                    Button("Open Marquee") { run("Opened Marquee on \(tv.name)", "appletvs/open") }
                    Button("Connect Marquee") { connect() }
                        .help("Open Marquee there and have it pair with this server")
                    Button("Unpair", role: .destructive) {
                        guard confirm("Unpair \(tv.name)?", button: "Unpair") else { return }
                        run("Unpaired \(tv.name)", "appletvs/remove")
                    }
                }
                .disabled(busy)
            }
        }
    }

    private func run(_ done: String, _ path: String) {
        busy = true
        Task {
            await model.perform(done) { try await model.api.post(path, ["player": tv.player]) }
            busy = false
        }
    }

    private func connect() {
        busy = true
        Task {
            await model.perform {
                let answer: OK = try await model.api.post("appletvs/connect", ["player": tv.player])
                if let message = answer.message { model.say(message) }
            }
            busy = false
        }
    }
}

/// An Apple TV found on the network: name the Marquee player it runs, pair, then type the PIN from the TV.
private struct FoundRow: View {
    let model: PanelModel
    let tv: FoundAppleTV
    let marquees: [String]
    let onPaired: () -> Void
    @State private var player = ""
    @State private var pairId: String?
    @State private var pin = ""
    @State private var busy = false

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading) {
                Text(tv.name).bold()
                Text(tv.address).font(.caption).foregroundStyle(.secondary)
            }
            .frame(minWidth: 160, alignment: .leading)
            Spacer()
            if pairId != nil {
                TextField("PIN on the TV", text: $pin).frame(width: 110)
                    .onChange(of: pin) { _, new in pin = String(new.filter(\.isNumber).prefix(4)) }
                    .onSubmit(finish)
                Button("Pair", action: finish).buttonStyle(.borderedProminent).disabled(busy || pin.count != 4)
                Button("Cancel") { pairId = nil }
            } else {
                TextField("Marquee player", text: $player).frame(width: 180)
                if !marquees.isEmpty {
                    Menu("") { ForEach(marquees, id: \.self) { name in Button(name) { player = name } } }
                        .menuIndicator(.visible).fixedSize()
                }
                Button(busy ? "Asking…" : tv.linked == nil ? "Pair" : "Pair Again", action: start)
                    .disabled(busy || player.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
        .onAppear { if player.isEmpty { player = tv.linked ?? tv.name } }
    }

    private func start() {
        busy = true
        Task {
            struct Answer: Decodable { let pairId: String }
            do {
                let answer: Answer = try await model.api.post("appletvs/pair", ["identifier": tv.identifier, "player": player])
                pairId = answer.pairId
                model.say("The TV is showing a PIN")
            } catch {
                model.say(error.localizedDescription, error: true)
            }
            busy = false
        }
    }

    private func finish() {
        guard let pairId, pin.count == 4 else { return }
        busy = true
        Task {
            let worked = await model.perform {
                let answer: OK = try await model.api.post("appletvs/pair/\(pairId)", ["pin": pin])
                if let message = answer.message { model.say(message) }
            }
            busy = false
            if worked {
                self.pairId = nil
                onPaired()
            }
        }
    }
}
