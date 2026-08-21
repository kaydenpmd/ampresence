# Context for Claude Code

The app is called **Ammy** on the Home Screen. The Xcode target, scheme, and
bundle ID are all still `AMPresence` / `com.local.ampresence` — deliberately,
so builds keep working and SideStore upgrades in place instead of installing a
second copy. Don't rename them.

Apple Music on iPhone → Discord Rich Presence on a Windows desktop.

```
iPhone ──https──▶ am.kaydenpmd.net (Cloudflare Tunnel) ──▶ relay.py ──IPC──▶ Discord desktop
```

Working as of Aug 2026. Don't redesign it — the shape is deliberate.

## Why it's built this way

Discord has **no Rich Presence SDK on iOS**. The official path is a local IPC
socket (`\\.\pipe\discord-ipc-0`) that only the desktop client exposes. Setting
presence from a phone directly would mean opening a Gateway WebSocket with a
user token — self-botting, against ToS, accounts get terminated.

The relay exists to avoid that. The phone never talks to Discord; it POSTs
now-playing JSON to a relay on the PC, and the relay uses the sanctioned IPC
path. **Any change that moves Discord communication onto the phone is wrong.**

Cost of this design: presence requires the PC awake with Discord desktop open.
That's accepted, not a bug to fix.

## Layout

```
project.yml                     XcodeGen spec — no .xcodeproj is committed
.github/workflows/build-ipa.yml CI producing an unsigned IPA
bridge/relay.py                 Desktop relay + Discord IPC client
bridge/ipc_test.py              Minimal pypresence test, no HTTP layer
AMPresence/
  AMPresenceApp.swift           SwiftUI entry point + settings screen
  PresenceController.swift      Wires playback changes to relay pushes
  NowPlayingMonitor.swift       MediaPlayer now-playing observation
  PresenceRelay.swift           HTTPS client for the relay
  KeepAlive.swift               Silent audio to survive backgrounding
```

## Gotchas already paid for — don't rediscover these

**pypresence enums, not ints.** `update()` calls `.value` on `activity_type`
and `status_display_type`. Passing `2` raises `AttributeError: 'int' object has
no attribute 'value'`, which the worker's generic handler misreads as a dropped
socket, producing an infinite connect/fail/reconnect loop. Use
`ActivityType.LISTENING` and `StatusDisplayType.*`. `push_with_fallback()`
exists to shed unsupported kwargs rather than tearing down the connection.

**Timestamp jitter.** `start` is recomputed on every push as `now - elapsed`,
so integer rounding makes it drift ±1s even when nothing changed. Comparing
payloads directly re-pushes on every heartbeat and visibly nudges the progress
bar. `_materially_different()` applies a 2-second tolerance to `start`/`end`.

**currentPlaybackTime is unreliable right after a track change.** It can report
the previous song's position for a second or two. `PresenceController` re-reads
elapsed at send time (not from the Combine-captured snapshot) and fires a
correction push 2.5s after every change. Removing that correction reintroduces
wrong progress bars on skip.

**Artwork matching.** iTunes Search with `limit=1` returns compilations and
remasters with the wrong cover. Current code pulls 12 results and scores
title/artist/album, weighting album heaviest (the artwork *is* the album
cover), and returns nothing below 0.55 — a confidently wrong cover is worse
than none. Known weak spot: singles where album name == track name.

**No .xcodeproj in the repo.** CI generates it with XcodeGen from
`project.yml`. `SWIFT_VERSION` is a *language mode* — valid values are
4.0/4.2/5.0/6.0. "5.9" is rejected.

## Build and deploy

Push to `main` triggers `.github/workflows/build-ipa.yml` on a `macos-26`
runner. It builds unsigned (AltStore/SideStore re-signs at install), zips the
`.app` into `Payload/`, and uploads `AMPresence-unsigned` as an artifact.

Repo is public — macOS runner minutes bill at 10× on private repos.

Install path is **SideStore**, not AltStore. AltStore's AltServer requires
iTunes *and* iCloud direct from Apple; the owner keeps the Microsoft Store
Apple apps, and harvesting the 2020 iCloud components produced "The provided
anisette data is invalid." SideStore needs only iTunes (Store version is fine)
and refreshes on-device, so it doesn't hit that wall. Don't suggest AltStore.

## Runtime config

Environment variables read by `relay.py`:

- `DISCORD_CLIENT_ID` — the Discord application ID. Its **name** is the text
  after "Listening to", so the app is named `Apple Music`.
- `RELAY_SECRET` — shared secret; must match the app's Secret field. It is
  declared here, not looked up from anywhere.
- `STATUS_LINE` — `name` / `state` / `details`, controls the compact
  member-list line. Defaults to `state` (artist).

The Discord application stays named `Apple Music`, not Ammy — that string is
what renders after "Listening to", and it should describe the source, not the
bridge.
- `RELAY_PORT` — defaults to 8787.

Relay binds `127.0.0.1` only; the tunnel is the sole ingress.

## Working with the owner

Limited coding experience — comfortable running commands and reading output,
not writing code. Prefers concise, concrete instructions over conceptual
explanation. Give **one command at a time** and wait for output; stacked
commands hide failures when an early one hangs.

Known outstanding tasks:
- Cloudflare tunnel token and `RELAY_SECRET` were exposed in screenshots and
  should be rotated.
- `relay.py` still needs a Task Scheduler entry (at logon, via `pythonw.exe`,
  in the interactive user session — not a Windows service, since Discord's
  named pipe belongs to the logged-in session).
