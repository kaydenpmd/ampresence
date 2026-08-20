import AVFoundation

/// iOS suspends foreground apps after ~30 seconds. The standard (if grubby)
/// workaround is to claim the `audio` background mode and play silence.
/// `.mixWithOthers` is essential — without it this fights Apple Music for the
/// audio session and stops your actual playback.
///
/// This is exactly the sort of thing App Review rejects. Fine for sideloading.
final class KeepAlive {
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private var running = false

    func start() {
        guard !running else { return }

        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
        try? session.setActive(true)

        guard let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 2),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 44_100)
        else { return }
        buffer.frameLength = buffer.frameCapacity // zero-filled = one second of silence

        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
        engine.mainMixerNode.outputVolume = 0

        do { try engine.start() } catch { return }
        node.scheduleBuffer(buffer, at: nil, options: .loops)
        node.volume = 0
        node.play()
        running = true
    }

    func stop() {
        guard running else { return }
        node.stop()
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        running = false
    }
}
