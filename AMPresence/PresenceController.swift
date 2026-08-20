import Foundation
import Combine

@MainActor
final class PresenceController: ObservableObject {
    @Published var endpoint = UserDefaults.standard.string(forKey: "relay_endpoint") ?? ""
    @Published var secret = UserDefaults.standard.string(forKey: "relay_secret") ?? ""
    @Published private(set) var linkStatus = "Idle"
    @Published private(set) var lastPushed = "—"

    let monitor = NowPlayingMonitor()

    private let keepAlive = KeepAlive()
    private var relay: PresenceRelay?
    private var bag = Set<AnyCancellable>()
    private var heartbeat: Task<Void, Never>?

    init() {
        monitor.$track
            .combineLatest(monitor.$isPlaying)
            .removeDuplicates { $0.0?.key == $1.0?.key && $0.1 == $1.1 }
            .debounce(for: .milliseconds(600), scheduler: RunLoop.main)
            .sink { [weak self] track, playing in
                self?.send(track: track, playing: playing)
            }
            .store(in: &bag)
    }

    func start() async {
        guard let relay = PresenceRelay(endpoint: endpoint, secret: secret) else {
            linkStatus = "Endpoint must be a valid https:// URL"
            return
        }
        UserDefaults.standard.set(endpoint, forKey: "relay_endpoint")
        UserDefaults.standard.set(secret, forKey: "relay_secret")

        self.relay = relay
        keepAlive.start()
        await monitor.start()

        guard monitor.authorized else {
            linkStatus = "Media library access denied"
            return
        }

        // Re-push periodically. The relay clears presence after 90s of
        // silence, so this doubles as a liveness signal.
        heartbeat = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard let self else { return }
                await MainActor.run {
                    self.monitor.refresh()
                    self.send(track: self.monitor.track, playing: self.monitor.isPlaying)
                }
            }
        }

        linkStatus = "Running"
    }

    func stop() async {
        heartbeat?.cancel()
        heartbeat = nil
        await relay?.push(track: nil, playing: false)
        relay = nil
        keepAlive.stop()
        linkStatus = "Idle"
        lastPushed = "—"
    }

    private func send(track: Track?, playing: Bool) {
        guard let relay else { return }
        let label = (track != nil && playing)
            ? "\(track!.title) — \(track!.artist)"
            : "Nothing playing"

        Task {
            let ok = await relay.push(track: track, playing: playing)
            await MainActor.run {
                self.lastPushed = label
                self.linkStatus = ok ? "Running" : "Relay unreachable"
            }
        }
    }
}
