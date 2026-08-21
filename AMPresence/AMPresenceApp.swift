import SwiftUI
import UIKit

/// What the app should do once it has started, based on how it was launched.
enum BounceTarget {
    case stay        // opened by tapping the icon — the person wants the UI
    case music       // reopen Apple Music
    case home        // drop to the Home Screen

    init(url: URL) {
        // ammy://music  → host is "music"
        switch url.host?.lowercased() {
        case "music": self = .music
        case "background", "home": self = .home
        default: self = .stay
        }
    }
}

@main
struct AMPresenceApp: App {
    @StateObject private var controller = PresenceController()

    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(controller)
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var controller: PresenceController
    @State private var running = false
    @State private var bounce: BounceTarget = .stay
    @State private var didAutoStart = false

    private var canAutoStart: Bool {
        !controller.endpoint.isEmpty && !controller.secret.isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Relay") {
                    TextField("https://am.kaydenpmd.net/now-playing",
                              text: $controller.endpoint)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    SecureField("Shared secret", text: $controller.secret)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }

                Section("Status") {
                    LabeledContent("Link", value: controller.linkStatus)
                    LabeledContent("Now playing", value: controller.lastPushed)
                    LabeledContent("Media access",
                                   value: controller.monitor.authorized ? "Granted" : "Not granted")
                }

                Section {
                    Button(running ? "Stop" : "Start") {
                        Task {
                            if running {
                                await controller.stop()
                                didAutoStart = true   // don't immediately restart
                            } else {
                                await controller.start()
                            }
                            running.toggle()
                        }
                    }
                    .disabled(!canAutoStart)
                }

                Section("Shortcuts") {
                    Text("ammy://music")
                        .font(.system(.footnote, design: .monospaced))
                    Text("Starts, then reopens Apple Music.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Text("ammy://background")
                        .font(.system(.footnote, design: .monospaced))
                    Text("Starts, then returns to the Home Screen.")
                        .font(.footnote).foregroundStyle(.secondary)
                }

                Section {
                    Text("Your PC must be awake with the Discord desktop app running. "
                         + "Presence clears automatically after 90 seconds of silence.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Ammy")
        }
        .task {
            await autoStartIfPossible()
        }
        .onOpenURL { url in
            bounce = BounceTarget(url: url)
            Task {
                // The URL can arrive either before or after .task runs, so
                // handle both orderings.
                if running {
                    await performBounce(justStarted: false)
                } else {
                    await autoStartIfPossible()
                }
            }
        }
    }

    @MainActor
    private func autoStartIfPossible() async {
        guard !didAutoStart, !running, canAutoStart else { return }
        didAutoStart = true
        await controller.start()
        running = true
        await performBounce(justStarted: true)
    }

    @MainActor
    private func performBounce(justStarted: Bool) async {
        guard case let target = bounce, target != .stay else { return }

        // Only wait when we've just started: the audio session needs a moment
        // to establish, and leaving too early can get the app suspended before
        // KeepAlive holds it. If it was already running there's nothing to wait
        // for, so bounce immediately and keep the interruption as short as
        // possible — this is the common case for a scheduled automation.
        if justStarted {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }

        switch target {
        case .music:
            if let url = URL(string: "music://") {
                await UIApplication.shared.open(url)
            }
        case .home:
            // Private API — equivalent to pressing Home. Fine for a sideloaded
            // build, may stop working on a future iOS. Failure is harmless:
            // the app simply stays in the foreground.
            UIApplication.shared.perform(Selector(("suspend")))
        case .stay:
            break
        }

        bounce = .stay
    }
}

extension BounceTarget: Equatable {}
