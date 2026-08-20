# AM Presence

Apple Music on iPhone → Discord Rich Presence on desktop, via a relay you host.

```
iPhone ──https──▶ Cloudflare Tunnel ──▶ relay.py ──IPC──▶ Discord desktop
```

No user token, no self-botting. The relay talks to Discord through the desktop
client's local IPC socket, which is the sanctioned Rich Presence path.

## 1. Discord application

Create one at `discord.com/developers/applications`. You only need its ID. The
application's **name** is the text that appears after "Listening to", so name it
`Apple Music`.

## 2. Desktop relay (Windows)

```
pip install pypresence
set DISCORD_CLIENT_ID=your_application_id
set RELAY_SECRET=paste_a_long_random_string
python bridge\relay.py
```

Then expose it. With `cloudflared` and your Cloudflare domain:

```
cloudflared tunnel create ampresence
cloudflared tunnel route dns ampresence music.yourdomain.com
cloudflared tunnel run --url http://localhost:8787 ampresence
```

The relay binds to `127.0.0.1` on purpose — the tunnel is the only way in, so
there's no port forwarding and nothing exposed on your LAN. The shared secret
is checked on every request; Cloudflare Access in front of it would be stronger
if you want defence in depth.

## 3. Build the IPA without a Mac

Push this repo to GitHub and run the **Build unsigned IPA** workflow. It spins
up a macOS runner, generates the Xcode project from `project.yml` via XcodeGen,
compiles with signing disabled, and uploads `AMPresence.ipa` as an artifact.

Signing is skipped because AltStore re-signs with your Apple ID at install time.
That also means no certificates in CI secrets.

Note that macOS runner minutes bill at 10× on private repos. A public repo is
free; if you'd rather keep it private, expect the minutes to add up.

Download the artifact, unzip, and sideload the `.ipa` with AltStore. Free Apple
ID means re-signing every 7 days — AltServer handles that while it's running.

## 4. Run it

Enter `https://music.yourdomain.com/now-playing` and your shared secret, tap
Start, grant media library access.

## Limits worth knowing

- **PC must be awake with Discord desktop open.** This is the real cost of
  avoiding self-botting — presence dies when your machine sleeps. The relay
  clears it after 90s of silence rather than leaving a stale song up.
- **Apple Music only.** `MPMusicPlayerController.systemMusicPlayer` sees the
  built-in Music app and nothing else. System-wide now-playing lives behind
  the private MediaRemote framework, which is entitlement-gated.
- **Cloud tracks are inconsistent.** `nowPlayingItem` is reliable for library
  content and usually fine for streamed catalog tracks, but does return nil
  sometimes. The 5s poll covers missed notifications, not nil.
- **Background survival is best-effort.** The silent-audio trick usually holds,
  but iOS can evict the app under memory pressure, and it dies on force quit.
- **Scrubbing drifts.** Timestamps recompute on track change and on the 30s
  heartbeat, so seeking leaves the progress bar wrong for up to half a minute.
- **Artwork depends on Discord's build.** Current desktop clients accept a
  plain https URL in `large_image`. If yours doesn't, upload art as registered
  assets on your Discord application and send keys instead.

## Files

```
project.yml                     XcodeGen spec — no .xcodeproj is committed
.github/workflows/build-ipa.yml CI that produces the unsigned IPA
bridge/relay.py                 Desktop relay + Discord IPC client
AMPresence/
  AMPresenceApp.swift           SwiftUI entry point and settings screen
  PresenceController.swift      Wires playback changes to relay pushes
  NowPlayingMonitor.swift       MediaPlayer now-playing observation
  PresenceRelay.swift           HTTPS client for the relay
  KeepAlive.swift               Silent audio to survive backgrounding
```
