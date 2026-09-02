import AVFoundation

/// iOS suspends foreground apps after ~30 seconds. The standard (if grubby)
/// workaround is to claim the `audio` background mode and play silence.
/// `.mixWithOthers` is essential — without it this fights Apple Music for the
/// audio session and stops your actual playback.
///
/// This is exactly the sort of thing App Review rejects. Fine for sideloading.
///
/// The silence has to *keep* playing. An interruption — a call, an alarm, Siri
/// — stops the engine, and nothing restarts it on its own. From then on the app
/// has no audio justifying its background time, so iOS suspends and eventually
/// kills it, minutes or hours later depending on memory pressure. That reads as
/// "iOS is being aggressive" rather than as a bug, which is what makes it worth
/// a comment: the observers below are the difference between a keepalive that
/// works in testing and one that survives a normal day of phone use.
final class KeepAlive {

    // Recreated wholesale after a media services reset, so these are `var`.
    private var engine = AVAudioEngine()
    private var node = AVAudioPlayerNode()

    private var running = false
    private var wired = false
    private var observers: [NSObjectProtocol] = []

    func start() {
        guard !running else { return }
        guard resume() else { return }
        running = true
        observe()
    }

    func stop() {
        guard running else { return }
        running = false
        removeObservers()
        node.stop()
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Bring session and engine back up. Idempotent, so it can be used both for
    /// the initial start and for recovery after an interruption.
    @discardableResult
    private func resume() -> Bool {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
        try? session.setActive(true)

        guard let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 2),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 44_100)
        else { return false }
        buffer.frameLength = buffer.frameCapacity // zero-filled = one second of silence

        // Attach and connect once per engine instance; doing it repeatedly on a
        // live graph is an error.
        if !wired {
            engine.attach(node)
            engine.connect(node, to: engine.mainMixerNode, format: format)
            wired = true
        }
        engine.mainMixerNode.outputVolume = 0

        do { try engine.start() } catch { return false }
        node.scheduleBuffer(buffer, at: nil, options: .loops)
        node.volume = 0
        node.play()
        return true
    }

    private func observe() {
        let nc = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()

        observers.append(nc.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: session, queue: .main
        ) { [weak self] note in
            guard let self, self.running else { return }
            guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw)
            else { return }

            // Deliberately ignoring `shouldResume`: that hint is about whether
            // *media* should resume. This is a keepalive, and it always should.
            if type == .ended { self.resume() }
        })

        // Media services can be reset out from under the app. Every audio
        // object is invalid afterwards, so rebuild the graph rather than
        // restarting the dead one.
        observers.append(nc.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: session, queue: .main
        ) { [weak self] _ in
            guard let self, self.running else { return }
            self.engine = AVAudioEngine()
            self.node = AVAudioPlayerNode()
            self.wired = false
            self.resume()
        })
    }

    private func removeObservers() {
        let nc = NotificationCenter.default
        observers.forEach(nc.removeObserver)
        observers.removeAll()
    }

    deinit { removeObservers() }
}
