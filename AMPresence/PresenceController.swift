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
    private let watchdog = SilenceWatchdog()
    private var relay: PresenceRelay?
    private var bag = Set<AnyCancellable>()
    private var heartbeat: Task<Void, Never>?
    private var correction: Task<Void, Never>?

    init() {
        monitor.$track
            .combineLatest(monitor.$isPlaying)
            .removeDuplicates { $0.0?.key == $1.0?.key && $0.1 == $1.1 }
            .debounce(for: .milliseconds(600), scheduler: RunLoop.main)
            .sink { [weak self] _, _ in
                // Deliberately ignore the captured values and re-read current
                // state at send time; see sendNow().
                self?.handleChange()
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

        // Before anything can be scheduled. An unauthorized center accepts
        // notification requests and delivers none of them, so skipping this
        // makes the watchdog look like it works right up until it matters.
        await watchdog.requestAuthorizationIfNeeded()

        await monitor.start()

        guard monitor.authorized else {
            linkStatus = "Media library access denied"
            return
        }

        heartbeat = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard let self else { return }
                await MainActor.run { self.sendNow() }
            }
        }

        linkStatus = "Running"
        handleChange()
    }

    func stop() async {
        heartbeat?.cancel(); heartbeat = nil
        correction?.cancel(); correction = nil
        await relay?.push(track: nil, playing: false)
        relay = nil
        keepAlive.stop()
        watchdog.cancel()   // stopping on purpose isn't a failure
        linkStatus = "Idle"
        lastPushed = "—"
    }

    /// Push immediately, then again shortly after. currentPlaybackTime is
    /// unreliable for a second or two following a track change — it can still
    /// report the previous song's position — so the first push may carry a bad
    /// elapsed value and the follow-up corrects the progress bar.
    private func handleChange() {
        sendNow()

        correction?.cancel()
        correction = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard !Task.isCancelled else { return }
            await MainActor.run { self?.sendNow() }
        }
    }

    private func sendNow() {
        guard let relay else { return }

        monitor.refresh()
        guard var track = monitor.track, monitor.isPlaying else {
            lastPushed = "Nothing playing"
            Task {
                let ok = await relay.push(track: nil, playing: false)
                await MainActor.run {
                    self.linkStatus = ok ? "Running" : "Relay unreachable"
                    // The watchdog measures whether the app is alive, not
                    // whether music is playing — so a successful "nothing
                    // playing" push counts just as much.
                    if ok { self.watchdog.postpone() }
                }
            }
            return
        }

        // Freshest possible position, taken at the moment of sending.
        track.elapsed = monitor.liveElapsed
        let label = "\(track.title) — \(track.artist)"

        Task {
            let ok = await relay.push(track: track, playing: true)
            await MainActor.run {
                self.lastPushed = label
                self.linkStatus = ok ? "Running" : "Relay unreachable"
                if ok { self.watchdog.postpone() }
            }
        }
    }
}
