import Foundation
import MediaPlayer

struct Track: Equatable {
    var title: String
    var artist: String
    var album: String
    var duration: TimeInterval
    var elapsed: TimeInterval

    /// Identity for change detection — elapsed is excluded on purpose.
    var key: String { "\(title)|\(artist)|\(album)" }
}

@MainActor
final class NowPlayingMonitor: ObservableObject {
    @Published private(set) var track: Track?
    @Published private(set) var isPlaying = false
    @Published private(set) var authorized = false

    private let player = MPMusicPlayerController.systemMusicPlayer
    private var pollTimer: Timer?

    func start() async {
        let status: MPMediaLibraryAuthorizationStatus = await withCheckedContinuation { cont in
            MPMediaLibrary.requestAuthorization { cont.resume(returning: $0) }
        }
        authorized = (status == .authorized)
        guard authorized else { return }

        player.beginGeneratingPlaybackNotifications()
        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(refresh),
                       name: .MPMusicPlayerControllerNowPlayingItemDidChange, object: player)
        nc.addObserver(self, selector: #selector(refresh),
                       name: .MPMusicPlayerControllerPlaybackStateDidChange, object: player)

        // Notifications are flaky for cloud/catalog tracks that aren't in the
        // local library, so poll as a safety net. 5s is a reasonable tradeoff.
        pollTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        refresh()
    }

    @objc func refresh() {
        isPlaying = (player.playbackState == .playing)

        guard let item = player.nowPlayingItem else {
            track = nil
            return
        }

        track = Track(
            title: item.title ?? "Unknown Track",
            artist: item.artist ?? item.albumArtist ?? "Unknown Artist",
            album: item.albumTitle ?? "",
            duration: item.playbackDuration,
            elapsed: player.currentPlaybackTime
        )
    }

    deinit {
        player.endGeneratingPlaybackNotifications()
    }
}
