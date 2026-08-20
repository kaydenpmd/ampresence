import Foundation

/// Ships now-playing state to the desktop relay. Replaces the old Gateway
/// client — the phone no longer talks to Discord at all.
actor PresenceRelay {
    private let endpoint: URL
    private let secret: String
    private let session: URLSession

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
        var body: [String: Any] = ["playing": playing && track != nil]

        if let track, playing {
            body["title"] = track.title
            body["artist"] = track.artist
            body["album"] = track.album
            body["duration"] = track.duration
            body["elapsed"] = track.elapsed
        }

        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(secret, forHTTPHeaderField: "X-Relay-Secret")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)

        guard let (_, response) = try? await session.data(for: req),
              let http = response as? HTTPURLResponse
        else { return false }

        return (200..<300).contains(http.statusCode)
    }
}
