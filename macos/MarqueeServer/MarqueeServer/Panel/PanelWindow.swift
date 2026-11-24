import AppKit
import SwiftUI

/// The control panel as a Mac window: the same tabs as the web panel, down the side.
struct PanelWindow: View {
    @Bindable var model: PanelModel
    @ObservedObject var server: ServerController

    var body: some View {
        NavigationSplitView {
            List(PanelTab.allCases, selection: $model.selection) { tab in
                Label(tab.label, systemImage: tab.symbol)
                    .badge(tab == .devices ? model.overview?.pairingRequests.count ?? 0 : 0)
                    .tag(tab)
            }
            .navigationSplitViewColumnWidth(min: 170, ideal: 190, max: 240)
        } detail: {
            detail
                .navigationTitle(model.tab.label)
                .toolbar {
                    ToolbarItem(placement: .automatic) {
                        if server.state == .running {
                            Pill(text: model.reachable ? "Live" : "Reconnecting…", tone: model.reachable ? .good : .bad)
                        }
                    }
                    ToolbarItem(placement: .automatic) {
                        Button {
                            NSWorkspace.shared.open(server.panelURL)
                        } label: {
                            Label("Open in Browser", systemImage: "safari")
                        }
                        .help("Open the web control panel")
                        .disabled(server.state != .running)
                    }
                }
        }
        .overlay(alignment: .bottom) {
            if let notice = model.notice { NoticeBanner(notice: notice) }
        }
        .animation(.easeOut(duration: 0.2), value: model.notice)
        .frame(minWidth: 860, minHeight: 560)
        .task(id: server.state) { await model.poll() }
        // A window of its own: show the app in the Dock and the app switcher while it's open
        .onAppear { NSApp.setActivationPolicy(.regular) }
        .onDisappear { NSApp.setActivationPolicy(.accessory) }
    }

    @ViewBuilder private var detail: some View {
        if server.state != .running {
            ContentUnavailableView {
                Label("The server isn't running", systemImage: "bolt.horizontal.circle")
            } description: {
                Text(server.statusText)
            } actions: {
                if case .settingUp = server.state {
                    ProgressView()
                } else {
                    Button("Start Server") { server.start() }
                }
            }
        } else if let overview = model.overview {
            content(overview)
        } else if !model.reachable {
            ContentUnavailableView("Can't reach the server", systemImage: "wifi.exclamationmark",
                                   description: Text("It may still be starting."))
        } else {
            ProgressView("Loading…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder private func content(_ overview: Overview) -> some View {
        switch model.tab {
        case .tags:
            // The tag list fills the window with its own table
            VStack(spacing: 0) {
                if !overview.pairingRequests.isEmpty {
                    PairingRequestsView(model: model, overview: overview).padding([.horizontal, .top], 16)
                }
                TagsView(model: model, overview: overview)
            }
        default:
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if !overview.canManage {
                        Text("You can look around from here. To change anything, use the panel on the server itself.")
                            .foregroundStyle(.secondary)
                    }
                    if model.tab != .devices, !overview.pairingRequests.isEmpty {
                        PairingRequestsView(model: model, overview: overview)
                    }
                    switch model.tab {
                    case .home:
                        ServerStatusView(model: model, overview: overview)
                        NowPlayingView(model: model, overview: overview)
                    case .devices:
                        PairingRequestsView(model: model, overview: overview)
                        DevicesView(model: model, overview: overview)
                        AppleTVsView(model: model, overview: overview)
                    case .music: MusicView(model: model, overview: overview)
                    case .cards: CardsView(model: model, overview: overview)
                    case .book: BookView(model: model, overview: overview)
                    case .sources: SourcesView(model: model, overview: overview)
                    case .tags: EmptyView()
                    }
                }
                .padding(20)
                .frame(maxWidth: 1100, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
        }
    }
}
