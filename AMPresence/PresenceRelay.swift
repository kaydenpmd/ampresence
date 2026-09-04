import Foundation

/// Ships now-playing state to the desktop relay. Replaces the old Gateway
/// client — the phone no longer talks to Discord at all.
actor PresenceRelay {
    private let endpoint: URL
    private let secret: String
    private let session: URLSession

    /// Track whose cover the relay has already been given. The JPEG is ~80 KB
    /// and the heartbeat fires every 30 seconds, so sending it every time would
    /// be about 10 MB an hour for one unchanging image. The relay stores it on
    /// disk under a hash of the track and reuses it, so once is enough.
    private var artworkSentFor: String?

    init?(endpoint: String, secret: String) {
        guard let url = URL(string: endpoint), url.scheme == "https" else { return nil }
        self.endpoint = url
        self.secret = secret

        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10
        config.waitsForConnectivity = true
        self.session = URLSession(configuration: config)
    }

    @discardableResult
    func push(track: Track?, playing: Bool) async -> Bool {
        // The relay stamps this onto every uptime entry. Whether the app
        // survives backgrounding is decided by code in *this* build, so the
        // build number is the axis worth attributing gaps to — without it the
        // log can't tell a regression from ordinary noise.
        var body: [String: Any] = [
            "playing": playing && track != nil,
            "app_version": Bundle.main.displayVersion,
        ]

        // Set only when this push actually carries the cover, so a failed
        // request doesn't mark it delivered.
        var artworkAttachedFor: String?

        if let track, playing {
            body["title"] = track.title
            body["artist"] = track.artist
            body["album"] = track.album
            body["duration"] = track.duration
            body["elapsed"] = track.elapsed

            // "0" is what local files report; the relay ignores it anyway, but
            // there's no point sending a value that can't resolve.
            if !track.storeID.isEmpty && track.storeID != "0" {
                body["store_id"] = track.storeID
            }

            if artworkSentFor != track.key, let jpeg = track.artworkJPEG {
                body["artwork_b64"] = jpeg.base64EncodedString()
                artworkAttachedFor = track.key
            }
        }

        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(secret, forHTTPHeaderField: "X-Relay-Secret")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)

        guard let (_, response) = try? await session.data(for: req),
              let http = response as? HTTPURLResponse
        else { return false }

        let ok = (200..<300).contains(http.statusCode)
        if ok, let delivered = artworkAttachedFor {
            artworkSentFor = delivered
        }
        return ok
    }
}
