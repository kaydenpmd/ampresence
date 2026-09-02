import Foundation
import UserNotifications

/// An inverted dead-man's switch.
///
/// A dead app cannot notify you that it died. A living one can leave a note
/// that goes off if it stops. So this schedules a notification 15 minutes out
/// and pushes the deadline back on every successful relay push: while Ammy is
/// alive the notification never arrives, and if iOS kills the app the pending
/// request stays in the notification daemon and fires on schedule. That is why
/// it survives force quit, eviction and reboot — it no longer depends on this
/// process existing. Don't replace it with something that tries to detect
/// death directly; there is nothing left running to do the detecting.
///
/// The premise only holds if a notification can be scheduled at all, and that
/// requires authorization. Requesting it is not optional: `add(_:)` on an
/// unauthorized center succeeds silently and delivers nothing, which looks
/// exactly like a working watchdog that never needed to fire.
@MainActor
final class SilenceWatchdog {

    /// Reusing one identifier means each schedule *replaces* the pending
    /// request instead of stacking hundreds of them.
    private static let requestID = "ammy.silence"

    /// Comfortably longer than the 30s heartbeat, so ordinary network jitter
    /// or a brief backgrounding never trips it.
    private static let delay: TimeInterval = 15 * 60

    private let center = UNUserNotificationCenter.current()
    private var authorized = false

    /// Ask once. Safe to call on every start — iOS only prompts the first time
    /// and returns the existing answer afterwards.
    func requestAuthorizationIfNeeded() async {
        authorized = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
    }

    /// Push the deadline back. Call after every successful push, including
    /// "nothing playing" ones — this measures whether the app is alive, not
    /// whether music is playing.
    func postpone() {
        guard authorized else { return }

        let content = UNMutableNotificationContent()
        content.title = "Ammy stopped"
        content.body = "No update for 15 minutes — iOS most likely suspended it. "
                     + "Open Ammy to start it again."
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: Self.requestID,
            content: content,
            trigger: UNTimeIntervalNotificationTrigger(timeInterval: Self.delay, repeats: false)
        )

        center.add(request, withCompletionHandler: nil)
    }

    /// Stopping on purpose is not a failure, so clear the note.
    func cancel() {
        center.removePendingNotificationRequests(withIdentifiers: [Self.requestID])
    }
}
