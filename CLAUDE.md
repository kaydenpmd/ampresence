# Context for Claude Code

The app is called **Ammy** on the Home Screen. The Xcode target, scheme, and
bundle ID are all still `AMPresence` / `com.local.ampresence` — deliberately,
so builds keep working and SideStore upgrades in place instead of installing a
second copy. Don't rename them.

Apple Music on iPhone → Discord Rich Presence on a Windows desktop.

```
iPhone ──https──▶ ammy.kaydenpmd.net (Cloudflare Tunnel) ──▶ relay.py ──IPC──▶ Discord desktop
```

Working as of September 2026. Don't redesign it — the shape is deliberate.

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
.github/workflows/build-ipa.yml CI producing an unsigned, versioned IPA
bridge/relay.py                 Desktop relay + Discord IPC client
bridge/ipc_test.py              Minimal pypresence test, no HTTP layer
AMPresence/
  AMPresenceApp.swift           SwiftUI entry point + settings screen
  PresenceController.swift      Wires playback changes to relay pushes
  NowPlayingMonitor.swift       MediaPlayer observation; store ID + cover art
  PresenceRelay.swift           HTTPS client for the relay
  KeepAlive.swift               Silent audio to survive backgrounding
  SilenceWatchdog.swift         Notification that fires if the app dies
```

**The running relay is not in the repo tree.** On the main PC the live copy is
a loose file at `C:\Users\links\Python Scripts\relay.py`, with `.env`,
`art_cache\`, `relay.log`, `ammy-uptime.log` and `install-task.ps1` beside it.
Both `_load_env_file()` and `ART_DIR` resolve relative to that folder, so a
`.env` in the repo's `bridge/` would never be read. Edit the running copy, then
sync `bridge/relay.py` to match — they have silently diverged before.

## Telling builds apart

Both halves are versioned. Check these before debugging anything.

**Relay** — `RELAY_VERSION` near the top of `relay.py`, currently `1.0.0`.
Readable three ways without the filename ever changing:

```
[init] relay 1.0.0          relay.log, every startup
GET /version                e.g. https://ammy.kaydenpmd.net/version
python relay.py --version
```

`/health` still returns exactly `ok` and nothing else — Shortcuts test for that
string, which is why the version got its own route.

**iOS app** — the Status section shows a **Version** row, e.g. `1.0 (7)`. The
build number is the CI run, and the artifact is named to match:
`Ammy-1.0-b7-a1b2c3d.ipa`. Matching numbers means the phone is running the
build you think it is. `MARKETING_VERSION` in `project.yml` is the only version
number a human sets; the build number climbs on its own.

Older notes told you to grep `relay.py` for the string `artwork_by_store_id` to
guess whether it was current. That hack is retired — use the version.

## Gotchas already paid for — don't rediscover these

**Silent failure is this project's recurring bug.** Three separate times, a
function returned `None` on a failure path without logging, and the empty log
read as "working" rather than "broken." Every artwork path now prints on
failure. **When adding a new failure path, log it** — an unexplained silence
costs days here.

**pypresence enums, not ints.** `update()` calls `.value` on `activity_type`
and `status_display_type`. Passing `2` raises `AttributeError: 'int' object has
no attribute 'value'`, which the worker's generic handler misreads as a dropped
socket, producing an infinite connect/fail/reconnect loop. Use
`ActivityType.LISTENING` and `StatusDisplayType.*`. `push_with_fallback()`
exists to shed unsupported kwargs rather than tearing down the connection.

**`push_with_fallback` matches quoted field names.** It identifies which field
to drop from `'x'` in "unexpected keyword argument 'x'". A bare substring test
is wrong: `state` is a substring of `state_url`, so an error about `state_url`
would shed the *artist line* and leave the real offender in place. Don't
"simplify" that back.

**currentPlaybackTime is unreliable right after a track change.** It can report
the previous song's position for a second or two. `PresenceController` re-reads
elapsed at send time (not from the Combine-captured snapshot) and fires a
correction push 2.5s after every change. Removing that correction reintroduces
wrong progress bars on skip.

**Timestamp jitter.** `start` is recomputed on every push as `now - elapsed`,
so rounding makes it drift ±1s even when nothing changed. Comparing payloads
directly re-pushes on every heartbeat and visibly nudges the progress bar.
`_materially_different()` applies a 2-second tolerance to `start`/`end`. See
the open playhead bug below — this mitigation is not sufficient.

**KeepAlive must survive interruptions.** The silent audio holds the app alive
only while its `AVAudioSession` is active. A call, alarm or Siri invocation
stops the engine, and nothing restarts it on its own — the app then has no
audio justifying its background time and iOS reclaims it minutes or hours
later. `KeepAlive` observes `interruptionNotification` and
`mediaServicesWereResetNotification` and rebuilds. Before that fix, the app
died silently for 39 hours straight (Aug 27–29).

**Notification permission is not optional.** `UNUserNotificationCenter.add()`
on an unauthorized center succeeds and delivers nothing — no error, no crash.
`SilenceWatchdog` was inert for its entire existence because nothing ever
called `requestAuthorization`. `PresenceController.start()` now does, before
scheduling anything.

**The silence warning is inverted on purpose.** `SilenceWatchdog` schedules a
notification 15 minutes out and re-schedules on every successful push, so it
never fires while Ammy is alive. A dead app can't notify you; a living one can
leave a note that goes off if it stops. Pending notifications live in iOS's
notification daemon, so this survives force quit, eviction and reboot. Don't
"fix" it by detecting death directly — there's nothing left running to detect
it with.

**No .xcodeproj in the repo.** CI generates it with XcodeGen from
`project.yml`. `SWIFT_VERSION` is a *language mode* — valid values are
4.0/4.2/5.0/6.0. "5.9" is rejected.

**Don't transcribe config from screenshots.** `DISCORD_CLIENT_ID` was once
copied from a screenshot with one digit misread, and Discord answered
`Error Code: 4000 Message: Client ID is Invalid` — which reads like a deleted
application, not a typo. Have PowerShell write the file from the live variables
instead, and print `.Length` rather than the value when checking secrets.

## Artwork

Resolution order, best first:

1. **Catalog ID from the phone** (`store_id` ← `playbackStoreID`) — exact
   lookup, no guessing. This is the normal path.
2. **JPEG uploaded by the phone** (`artwork_b64`) — exact, for tracks with no
   catalog ID. Requires `PUBLIC_BASE`; the relay caches it under a hash of the
   track and serves it at `/art/<hash>.jpg`.
3. **Fuzzy iTunes Search** — best effort, and the source of most historical
   grief.

**History worth knowing.** For most of this project's life the iOS `Track`
struct carried neither `playbackStoreID` nor cover art, so paths 1 and 2 were
unreachable dead code and *every* track went through fuzzy search. Tracks
missing from the iTunes Store search index — common for independent and recent
releases — got no cover at all, and others got confidently wrong ones (a Wiz
Khalifa single matched to *Rolling Papers 2* at 0.48). Fixed September 2026 by
sending both fields from `NowPlayingMonitor`.

**The phone sends the JPEG once per track, not per heartbeat** — it's ~80 KB
and the heartbeat is every 30s. `existing_uploaded_artwork()` reuses the file
already on disk, without which the cover would appear on the first push and
vanish on the next.

**A `weak match` line now means the phone didn't send a store ID** for that
track. With the ID present the fuzzy path never runs, so those lines have
become a signal about the iOS side rather than about Apple's search index.

`ART_MIN_SCORE` defaults to **0.35**, not the 0.55 an earlier version of this
document claimed.

## Clickable presence

Discord supports hyperlinking activity text and artwork, and pypresence 4.6.2
accepts all of it:

| Field | Opens from | Source |
|---|---|---|
| `details_url` | the title line | `trackViewUrl` |
| `state_url` | the artist line | `artistViewUrl` |
| `large_url` | the cover art | `collectionViewUrl` |

All three come out of the same iTunes lookup that fetches artwork, so links
cost no extra requests. They are populated **only from the exact store-ID
lookup, never from fuzzy matching** — a near-miss cover is a cosmetic
annoyance, but a link that opens the wrong song is a broken promise.

Album name is `large_text`, which Discord renders as *both* the cover tooltip
and a visible third line on the card — one field, two places, and they can't be
separated. It's off by default behind `SHOW_ALBUM`. Spotify's presence shows a
tooltip with no third line, but that's a first-party card Discord special-cases
(it also carries a "Play on Spotify" button); third-party RPC gets the generic
renderer and doesn't get to choose.

## Build and deploy

Push to `main` triggers `.github/workflows/build-ipa.yml` on a `macos-26`
runner. It reads `MARKETING_VERSION` out of `project.yml`, passes
`CURRENT_PROJECT_VERSION=${{ github.run_number }}`, builds unsigned
(SideStore re-signs at install), and uploads `Ammy-<version>-b<run>-<sha>`.

Repo is public — macOS runner minutes bill at 10× on private repos.

Install path is **SideStore**, not AltStore. AltStore's AltServer requires
iTunes *and* iCloud direct from Apple; the owner keeps the Microsoft Store
Apple apps, and harvesting the 2020 iCloud components produced "The provided
anisette data is invalid." SideStore needs only iTunes (Store version is fine)
and refreshes on-device, so it doesn't hit that wall. Don't suggest AltStore.

## Autostart

Installed on the main PC as the logon Scheduled Task **`Ammy Relay`**, via
`install-task.ps1` beside the running `relay.py`: `pythonw.exe relay.py`, 30s
delay, `-LogonType Interactive`, `-RunLevel Limited`.

Interactive is load-bearing. "Run whether user is logged on or not" registers a
session 0 task that starts cleanly, listens on 8787, and never reaches
Discord's IPC pipe — a failure that looks like a Discord problem, not a task
problem.

```powershell
Start-ScheduledTask -TaskName "Ammy Relay"
Stop-ScheduledTask  -TaskName "Ammy Relay"
Get-ScheduledTaskInfo -TaskName "Ammy Relay" | Select LastRunTime,LastTaskResult
```

`LastTaskResult` `267009` means running. `Get-Process pythonw` is an unreliable
check — `relay.log` is the real evidence. `pythonw.exe` resolves to the
WindowsApps app-execution alias, a zero-byte reparse point; this was expected to
break under Task Scheduler and **does not**. Don't spend time rewiring it.

The task inherits no variables from any PowerShell window, which is why `.env`
is mandatory rather than convenient.

## Runtime config

`_load_env_file()` reads `.env` **from the folder containing `relay.py`** and
returns silently if absent. Real environment variables still win — and one set
in a PowerShell window applies only to a relay launched from that window, which
is why a value can look set in one shell and be empty to the running process.

- `DISCORD_CLIENT_ID` — the Discord application ID. Its **name** is the text
  after "Listening to", so the application stays named `Apple Music`, not Ammy:
  that string should describe the source, not the bridge.
- `RELAY_SECRET` — shared secret; must match the app's Secret field.
- `PUBLIC_BASE` — e.g. `https://ammy.kaydenpmd.net`. **Required** for uploaded
  artwork: Discord's CDN fetches the image itself and can't reach 127.0.0.1.
- `STATUS_LINE` — `name` / `state` / `details`, the compact member-list line.
  Defaults to `state` (artist).
- `SHOW_ALBUM` — `1` restores the album name. Default off; see above.
- `RELAY_PORT` — defaults to 8787.
- `ART_MIN_SCORE` — fuzzy floor, default 0.35. `0` always takes the best match.
- `ART_DIR` — uploaded-art cache, default `art_cache`.
- `UPTIME_LOG` — phone check-in gaps, default `ammy-uptime.log`.
  `python relay.py --summary` reads it back.

Under `pythonw.exe` there is no console and `sys.stdout` is None, so `relay.py`
redirects output to `relay.log` in the same folder. That file is the primary
diagnostic.

Relay binds `127.0.0.1` only; the tunnel is the sole ingress. `/art/` is
deliberately unauthenticated — Discord's CDN can't send the secret header, and
filenames are hashes, so they aren't enumerable.

Gap logging exists to answer whether iOS actually kills the app in the
background. Gaps are classified as phone-silent versus relay-was-down so a PC
reboot isn't miscounted as the app dying.

## Where things stand (September 2026)

Working and verified: autostart, artwork via store ID, clickable title/artist/
cover, album line removed, notification permission granted, versioning on both
halves.

**Open — playhead jitter and lag.** The Discord progress bar runs several
seconds behind Apple Music and nudges around. The relay sets
`start = now - elapsed`, so Discord displays exactly the `elapsed` the phone
reported, ticking forward from there — a stale read leaves the bar behind until
the next push corrects it, and `currentPlaybackTime` on a backgrounded app is
exactly where staleness comes from. Recomputing `start` every push also drags
the anchor around, which is the jitter. Useful property: the error runs one
direction only, since a stale read makes the position look *earlier*, never
later. Likely fix is to anchor `start` once per track, re-anchor only on a
genuine seek, and pick the anchor implying the furthest-along position across
recent samples.

**Open — rotate exposed secrets.** `RELAY_SECRET` has appeared in several
screenshots and the Cloudflare tunnel token in one. Rotating `RELAY_SECRET`
means changing `.env` and the iOS app's Secret field together. The tunnel token
lives in the cloudflared Windows service's registry `ImagePath`, not in a
`config.yml` — token-based installs have no config file.

**Open — remove the laptop's dead cloudflared connector**, so Cloudflare stops
load-balancing to a host with no relay listening. `am.kaydenpmd.net` returns
Cloudflare **1033**; `ammy.kaydenpmd.net` returns `ok`.

**Untested — the uploaded-JPEG path.** `art_cache\` is still empty because
every track played so far has been in the catalog. It's a genuine fallback now
rather than the only hope, but it has never actually run.

## Working with the owner

Limited coding experience — comfortable running commands and reading output,
not writing code. Prefers concise, concrete instructions over conceptual
explanation. Give **one command at a time** and wait for output; stacked
commands hide failures when an early one hangs.

There is no local clone of this repo on the PC. Changes reach GitHub by
uploading files through the web interface, so hand over complete files rather
than diffs or patch fragments.

**Verify, don't assert.** This project has burned several rounds on confident
wrong answers — that Discord couldn't hyperlink activity text (it can:
`details_url`, `state_url`, `large_url`), that a bug was in one place when the
logs hadn't been read yet. Read the file, read the log, check the API. When the
owner says something works, believe them and go look. Also: GitHub's tree and
contents API endpoints have served **stale cached listings** here, showing a
week-old file set as current — fetch `raw.githubusercontent.com` directly
instead of trusting a listing.
