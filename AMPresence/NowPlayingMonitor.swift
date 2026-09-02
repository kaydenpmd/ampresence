import Foundation
import MediaPlayer
import UIKit

struct Track: Equatable {
    var title: String
    var artist: String
    var album: String
    var duration: TimeInterval
    var elapsed: TimeInterval

    /// Apple Music catalog ID. The relay uses this for an exact artwork lookup
    /// with no fuzzy matching. Local files report "0".
    var storeID: String

    /// The cover as it exists on the phone, already JPEG-encoded. This is the
    /// only artwork source that always works: the iTunes Store search index the
    /// relay falls back to does not contain every track on Apple Music, and
    /// smaller/independent releases are routinely missing from it.
    var artworkJPEG: Data?

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

    // refresh() runs on every playback notification and every 5s poll.
    // Re-encoding a JPEG that often is pure waste, so keep the last one.
    private var artworkKey: String?
    private var artworkData: Data?

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

        let title = item.title ?? "Unknown Track"
        let artist = item.artist ?? item.albumArtist ?? "Unknown Artist"
        let album = item.albumTitle ?? ""

        track = Track(
            title: title,
            artist: artist,
            album: album,
            duration: item.playbackDuration,
            elapsed: player.currentPlaybackTime,
            storeID: item.playbackStoreID,
            artworkJPEG: artwork(for: item, key: "\(title)|\(artist)|\(album)")
        )
    }

    /// Cover art for the current item, encoded once per track.
    ///
    /// Artwork for a cloud track can be nil on the first read and populated a
    /// moment later, so a nil result is cached as "not yet" rather than "none":
    /// the next poll tries again.
    private func artwork(for item: MPMediaItem, key: String) -> Data? {
        if artworkKey == key, let cached = artworkData { return cached }

        guard let image = item.artwork?.image(at: CGSize(width: 512, height: 512)),
              let jpeg = image.jpegData(compressionQuality: 0.8)
        else { return nil }

        artworkKey = key
        artworkData = jpeg
        return jpeg
    }

    /// Live read, not the snapshot stored on `track`. Immediately after a
    /// track change this can still report the previous song's position, which
    /// is why PresenceController re-sends a correction a few seconds later.
    var liveElapsed: TimeInterval {
        player.currentPlaybackTime
    }

    deinit {
        player.endGeneratingPlaybackNotifications()
    }
}
