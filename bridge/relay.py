#!/usr/bin/env python3
"""
Desktop half of the Apple Music -> Discord bridge.

Accepts now-playing pushes from the phone over HTTP and forwards them to the
Discord desktop client through its local IPC socket. That socket is Discord's
sanctioned Rich Presence channel, so no user token is involved anywhere.

    pip install pypresence
    set DISCORD_CLIENT_ID=your_application_id
    set RELAY_SECRET=some_long_random_string
    python relay.py

Then expose it:  cloudflared tunnel --url http://localhost:8787
"""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
import os
import pathlib
import re
import sys
import unicodedata
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pypresence import Presence

try:
    # Raised when Discord accepts the connection but refuses the payload. That
    # is emphatically not a dead socket, and treating it as one costs a
    # reconnect cycle per attempt while presence sits empty.
    from pypresence.exceptions import DiscordError, InvalidArgument, ServerError
    PAYLOAD_REJECTED: tuple = (DiscordError, InvalidArgument, ServerError)
except ImportError:
    PAYLOAD_REJECTED = ()

try:
    # Added in pypresence 4.3.0. update() calls .value on this, so a plain
    # int raises AttributeError rather than being coerced.
    from pypresence import ActivityType
    LISTENING = ActivityType.LISTENING
except ImportError:
    LISTENING = None

try:
    # Controls which line Discord shows in the compact member-list view:
    # NAME = the app name ("Apple Music"), STATE = artist, DETAILS = title.
    from pypresence import StatusDisplayType
    _DISPLAY_CHOICES = {
        "name": StatusDisplayType.NAME,
        "state": StatusDisplayType.STATE,
        "details": StatusDisplayType.DETAILS,
    }
except ImportError:
    _DISPLAY_CHOICES = {}

def _load_env_file() -> None:
    """Read KEY=value lines from a .env next to this script.

    Environment variables set in a PowerShell window vanish when it closes,
    which makes Task Scheduler awkward and means retyping secrets after every
    reboot. A file next to the script survives both. Real environment
    variables still win, so nothing here overrides an explicit setting."""
    path = pathlib.Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
SECRET = os.environ.get("RELAY_SECRET", "").strip()
PORT = int(os.environ.get("RELAY_PORT", "8787"))

# Bump on any behaviour change. The filename deliberately never changes — it's
# referenced by the Scheduled Task and by anything that has been copied around —
# so this is the only way to tell two copies apart. Printed at startup, served
# at /version, and available as `python relay.py --version`.
#
# Answering "is the installed copy current?" used to mean grepping for a
# function name that happened to be recent; this replaces that.
#
#   1.0.0  first versioned build. Artwork via store ID and phone-uploaded JPEG,
#          clickable title/artist/cover, album tooltip behind SHOW_ALBUM,
#          logging on every artwork failure path.
#   1.0.1  dropped connections log one line instead of a traceback.
#   1.1.0  silences are logged when they start, not only when the phone
#          returns — a death that is never noticed is now still recorded.
#   1.2.0  playhead anchored per track instead of recomputed every push, so
#          the progress bar stops twitching and converges on the true position.
#   1.2.1  the anchor is paired with the push's arrival time instead of the
#          current time. build_payload runs every second against a reading the
#          phone only refreshes every thirty; using time.time() slid `start`
#          forward a second per second, which was the actual rubberbanding.
#   1.3.0  one-character titles no longer knock presence offline. Discord
#          requires 2+ characters in details/state and rejects the activity
#          otherwise; the worker read that as a dead socket and reconnect-looped
#          until the song changed. Fields are padded, payload rejections are
#          distinguished from connection loss, and the retry path can't spin.
RELAY_VERSION = "1.3.0"

# Which field shows on the one-line member-list view: name / state / details.
STATUS_LINE = os.environ.get("STATUS_LINE", "state").strip().lower()

# The album name is sent as large_text, which Discord renders as the tooltip on
# the cover art. Off by default: it's only discoverable by hovering, and when
# the artwork came from a fuzzy match the tooltip confidently states an album
# that may not be the right one. Set SHOW_ALBUM=1 to bring it back.
SHOW_ALBUM = os.environ.get("SHOW_ALBUM", "0").strip().lower() in ("1", "true", "yes", "on")

# Minimum confidence before artwork is used at all. Deliberately low: a
# near-miss cover still beats a blank one, and the title and artist are shown
# as text anyway. The floor only exists to catch tracks that aren't in the
# iTunes catalog, where every result is unrelated and showing one would be
# actively misleading. Set to 0 to always use the best available match.
ART_MIN_SCORE = float(os.environ.get("ART_MIN_SCORE", "0.35"))

# Where uploaded cover art from non-catalog tracks is kept, and served from.
ART_DIR = pathlib.Path(os.environ.get("ART_DIR", "art_cache")).resolve()
ART_DIR.mkdir(parents=True, exist_ok=True)
ART_CACHE_LIMIT = 60          # files; oldest are pruned beyond this
ART_MAX_BYTES = 3 * 1024 * 1024

# Public base URL of the relay, needed because Discord's CDN fetches the image
# itself and can't reach 127.0.0.1. Derived from the endpoint the phone uses.
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")

# Append-only record of when the phone stopped checking in, so the question
# "does iOS actually kill this?" becomes data instead of speculation.
UPTIME_LOG = pathlib.Path(os.environ.get("UPTIME_LOG", "ammy-uptime.log"))
GAP_THRESHOLD = 90.0          # seconds; the phone pushes every 30
RELAY_STARTED_AT = time.time()

# Clear presence if the phone stops reporting — covers force-quit and the app
# getting evicted in the background.
IDLE_TIMEOUT = 90.0

# Discord rate limits activity updates. Coalesce rapid changes (skipping
# through a playlist) rather than firing one call per skip.
MIN_PUSH_GAP = 3.0

# How far the reported position may move before it counts as a deliberate seek
# rather than a stale read. Observed staleness has been a few seconds; a seek
# worth reacting to is usually much larger.
PLAYHEAD_SEEK_TOLERANCE = 10.0

# Grace period after a track change during which any reading is accepted.
# currentPlaybackTime can still be reporting the previous song for a second or
# two, and PresenceController fires a correction 2.5s in — that correction has
# to be able to move the anchor backwards, which the steady-state rule forbids.
PLAYHEAD_SETTLE_WINDOW = 6.0


class State:
    """Desired presence, written by HTTP threads, read by the RPC worker."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.track: dict | None = None
        self.updated_at = 0.0

    def set(self, track: dict | None) -> None:
        with self.lock:
            self.track = track
            self.updated_at = time.time()

    def get(self) -> tuple[dict | None, float]:
        with self.lock:
            return self.track, self.updated_at


state = State()
_artwork_cache: dict[str, str] = {}

# Set when the relay notices the phone has stopped checking in, cleared when it
# comes back. Written only by the RPC worker thread, so no lock is needed.
_silence_logged_at: float | None = None




def _log_line(text: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{stamp}  {text}"
    print(f"[uptime] {text}")
    try:
        with UPTIME_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _format_gap(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def note_checkin(previous: float) -> None:
    """Called on every push from the phone. If there was a meaningful silence
    beforehand, record it — and say whether the relay itself was down for it,
    because a PC reboot is not the phone dying and shouldn't be counted as one."""
    now = time.time()
    if previous <= 0:
        return

    gap = now - previous
    if gap < GAP_THRESHOLD:
        return

    relay_uptime = now - RELAY_STARTED_AT
    if relay_uptime < gap:
        _log_line(f"gap {_format_gap(gap)}  relay was down for "
                  f"{_format_gap(gap - relay_uptime)} of it")
    else:
        _log_line(f"gap {_format_gap(gap)}  phone silent, relay up throughout")


def note_silence(last_seen: float) -> None:
    """Record the moment the phone stops checking in, rather than waiting for
    it to come back.

    `note_checkin()` can only log a gap once a push arrives, which means a death
    you never notice is never written down at all — the log stays silent and
    looks healthy. This is the other half: the relay watches the clock and
    writes the silence the moment it starts.

    The check works whether or not music was playing, because `updated_at` is
    stamped on every push including `playing: false` ones. That distinction
    matters: a paused app keeps checking in, a dead one doesn't."""
    global _silence_logged_at

    if last_seen <= 0:          # nothing has ever checked in
        return

    silent_for = time.time() - last_seen

    if silent_for <= GAP_THRESHOLD:
        _silence_logged_at = None
        return

    if _silence_logged_at == last_seen:
        return                  # already recorded this silence

    _silence_logged_at = last_seen
    when = time.strftime("%H:%M:%S", time.localtime(last_seen))
    _log_line(f"phone stopped checking in  last seen {when}")


def print_summary() -> None:
    """python relay.py --summary"""
    if not UPTIME_LOG.exists():
        print("No uptime log yet.")
        return

    starts, silences, phone_gaps, mixed_gaps = 0, 0, [], []
    for line in UPTIME_LOG.read_text(encoding="utf-8").splitlines():
        if "relay started" in line:
            starts += 1
        elif "stopped checking in" in line:
            silences += 1
        elif "gap " in line:
            try:
                hhmmss = line.split("gap ")[1].split()[0]
                h, m, s = (int(x) for x in hhmmss.split(":"))
                seconds = h * 3600 + m * 60 + s
            except (IndexError, ValueError):
                continue
            (phone_gaps if "phone silent" in line else mixed_gaps).append(seconds)

    print(f"relay starts            : {starts}")
    print(f"silences detected live  : {silences}")
    unreturned = silences - len(phone_gaps) - len(mixed_gaps)
    if unreturned > 0:
        print(f"  never came back       : {unreturned}   <- app died and stayed dead")
    print(f"phone-only gaps         : {len(phone_gaps)}")
    if phone_gaps:
        print(f"  longest               : {_format_gap(max(phone_gaps))}")
        print(f"  median                : "
              f"{_format_gap(sorted(phone_gaps)[len(phone_gaps) // 2])}")
        print(f"  total silent time     : {_format_gap(sum(phone_gaps))}")
    print(f"gaps including downtime : {len(mixed_gaps)}")
    if not phone_gaps:
        print("\nNo unexplained phone gaps recorded — backgrounding is holding.")


# Apple Music page URLs keyed by store ID, filled in as a side effect of the
# artwork lookup below — the same response carries both, so links cost no extra
# requests. Deliberately only populated from the exact store-ID lookup: the
# fuzzy search also returns these fields, but a link is worse than artwork when
# the match is wrong. A near-miss cover is a cosmetic annoyance; a link that
# opens the wrong song is a broken promise.
_links_cache: dict[str, dict[str, str]] = {}


def catalog_links(store_id: str) -> dict[str, str]:
    """Apple Music URLs for a store ID, if its lookup has already run."""
    return _links_cache.get(store_id, {})


def artwork_by_store_id(store_id: str) -> tuple[str | None, str]:
    """Exact lookup using the catalog ID the phone already knows. No fuzzy
    matching, no threshold — either the ID resolves or it doesn't."""
    key = f"id:{store_id}"
    if key in _artwork_cache:
        url, matched = _artwork_cache[key]
        return (url or None), matched

    art, matched_album = "", ""
    try:
        query = urllib.parse.urlencode({"id": store_id, "entity": "song"})
        with urllib.request.urlopen(
            f"https://itunes.apple.com/lookup?{query}", timeout=6
        ) as resp:
            payload = json.load(resp)

        results = payload.get("results") or []
        if results:
            entry = results[0]

            _links_cache[store_id] = {
                name: url for name, url in (
                    ("song", entry.get("trackViewUrl") or ""),
                    ("artist", entry.get("artistViewUrl") or ""),
                    ("album", entry.get("collectionViewUrl") or ""),
                ) if url
            }

            if entry.get("artworkUrl100"):
                art = entry["artworkUrl100"].replace("100x100bb", "512x512bb")
                matched_album = entry.get("collectionName") or ""
            else:
                # Resolved, but the catalog entry carries no cover. Distinct
                # from "not in catalog" — links still work here.
                print(f"[art] store id {store_id} resolved but has no artwork")
        else:
            print(f"[art] store id {store_id} not in catalog")
    except Exception as exc:
        print(f"[art] id lookup failed for {store_id}: {exc}")

    _artwork_cache[key] = (art, matched_album)
    return (art or None), matched_album


def store_uploaded_artwork(track_key: str, b64: str) -> str | None:
    """Persist cover art sent by the phone and return a publicly fetchable URL.
    Used for tracks that aren't in the catalog at all — local files, iTunes
    Match uploads — where no lookup can possibly succeed."""
    if not PUBLIC_BASE:
        return None

    try:
        blob = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None

    if not blob or len(blob) > ART_MAX_BYTES:
        return None
    # Cheap sanity check: JPEG magic bytes. Avoids writing arbitrary uploads.
    if not blob.startswith(b"\xff\xd8\xff"):
        return None

    name = hashlib.sha256(track_key.encode()).hexdigest()[:20] + ".jpg"
    path = ART_DIR / name
    if not path.exists():
        path.write_bytes(blob)
        _prune_art_cache()

    return f"{PUBLIC_BASE}/art/{name}"


def existing_uploaded_artwork(track_key: str) -> str | None:
    """URL for a cover the phone uploaded earlier in this track, if it's still
    on disk.

    The phone sends the JPEG once per track rather than on every 30s heartbeat,
    so most pushes arrive with no artwork attached. Without this the cover would
    appear on the first push and vanish on the next one. The filename is a pure
    function of the track key, so no extra bookkeeping is needed."""
    if not PUBLIC_BASE:
        return None
    name = hashlib.sha256(track_key.encode()).hexdigest()[:20] + ".jpg"
    return f"{PUBLIC_BASE}/art/{name}" if (ART_DIR / name).exists() else None


def _prune_art_cache() -> None:
    files = sorted(ART_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    for stale in files[:-ART_CACHE_LIMIT]:
        try:
            stale.unlink()
        except OSError:
            pass


def _normalize(text: str) -> str:
    """Fold case, strip bracketed qualifiers like (feat. X) / [Remix] and
    punctuation, so 'Song (Remastered 2011)' and 'Song' compare closely.

    Also drops release-type suffixes: Apple Music's library metadata says
    'Album' where the catalog says 'Album - Single', and that difference alone
    used to sink an otherwise perfect match."""
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(
        r"\s*[-–—]\s*(single|ep|deluxe|remastered|remaster|"
        r"deluxe edition|special edition|expanded edition|"
        r"original motion picture soundtrack|bonus track version)\b.*",
        " ", text,
    )
    text = re.sub(r"\b(feat|ft|featuring|with)\b.*", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _score(result: dict, title: str, artist: str, album: str) -> float:
    """Album is weighted heavily because the artwork *is* the album cover —
    the right song off the wrong release still gives you the wrong image."""
    title_s = _similarity(result.get("trackName", ""), title)
    artist_s = _similarity(result.get("artistName", ""), artist)
    if not album:
        return 0.6 * title_s + 0.4 * artist_s
    album_s = _similarity(result.get("collectionName", ""), album)
    return 0.40 * title_s + 0.25 * artist_s + 0.35 * album_s


def artwork_url(title: str, artist: str, album: str = "") -> tuple[str | None, str]:
    """Public iTunes Search lookup. Returns (url, matched_album).

    Ranks candidates and takes the best one rather than demanding a close
    match — a near-miss cover beats a blank one, since the song and artist are
    shown as text regardless. Only a score below ART_MIN_SCORE is rejected,
    which means nothing in the catalog resembles this track at all and any
    cover shown would be actively misleading."""
    key = f"{artist}|{title}|{album}"
    if key in _artwork_cache:
        url, matched = _artwork_cache[key]
        return (url or None), matched

    # Including the album narrows a huge number of near-duplicate releases.
    term = " ".join(x for x in (artist, title, album) if x)
    query = urllib.parse.urlencode(
        {"term": term, "entity": "song", "limit": 12}
    )

    art, matched_album = "", ""
    try:
        with urllib.request.urlopen(
            f"https://itunes.apple.com/search?{query}", timeout=6
        ) as resp:
            payload = json.load(resp)

        results = [r for r in (payload.get("results") or []) if r.get("artworkUrl100")]
        if results:
            # Rank with the album included, but score the winner both ways —
            # library and catalog album strings differ often enough that the
            # album shouldn't be able to veto an otherwise obvious match.
            best = max(results, key=lambda r: _score(r, title, artist, album))
            confidence = max(
                _score(best, title, artist, album),
                _score(best, title, artist, ""),
            )

            if confidence >= ART_MIN_SCORE:
                art = best["artworkUrl100"].replace("100x100bb", "512x512bb")
                matched_album = best.get("collectionName") or ""
                if confidence < 0.6:
                    print(f"[art] weak match ({confidence:.2f}): "
                          f"{title} \u2014 {artist} \u2192 {matched_album}")
            else:
                print(f"[art] nothing related ({confidence:.2f}): "
                      f"{title} \u2014 {artist}")
        else:
            # Zero usable results. Without this the function returns None having
            # printed nothing, which reads as success and hides the real cause:
            # the track isn't in the iTunes Store search index at all.
            print(f"[art] no catalog results: {term}")
    except Exception as exc:
        print(f"[art] lookup failed for {title}: {exc}")

    _artwork_cache[key] = (art, matched_album)
    return (art or None), matched_album


def rpc_worker() -> None:
    """Owns the Discord connection. pypresence runs its own event loop, so
    every call has to come from this one thread."""
    rpc: Presence | None = None
    last_payload: dict | None = None
    last_push = 0.0

    while True:
        if rpc is None:
            try:
                rpc = Presence(CLIENT_ID)
                rpc.connect()
                print("[rpc] connected to Discord")
                last_payload = None
            except Exception as exc:
                print(f"[rpc] Discord not reachable ({exc}); retrying in 10s")
                rpc = None
                time.sleep(10)
                continue

        track, updated_at = state.get()
        note_silence(updated_at)
        if track and time.time() - updated_at > IDLE_TIMEOUT:
            track = None
        if track is None:
            playhead.reset()

        payload = build_payload(track, updated_at) if track else None

        should_push = _materially_different(payload, last_payload) and (
            time.time() - last_push >= MIN_PUSH_GAP
        )

        if should_push:
            try:
                if payload is None:
                    rpc.clear()
                else:
                    payload = push_with_fallback(rpc, payload)
                last_payload = payload
                last_push = time.time()
                label = payload["details"] if payload else "cleared"
                print(f"[rpc] {label}")
            except PAYLOAD_REJECTED as exc:
                # The socket is fine; Discord just refused this activity. Tearing
                # the connection down would reconnect and re-send the same bad
                # payload forever, with presence empty the whole time. Record it
                # as sent so the worker moves on to the next track instead.
                print(f"[rpc] Discord refused the payload, skipping it ({exc})")
                last_payload = payload
                last_push = time.time()
            except Exception as exc:
                print(f"[rpc] lost connection ({exc})")
                try:
                    rpc.close()
                except Exception:
                    pass
                rpc = None
                time.sleep(1)   # never spin: reconnecting has a real cost
                continue

        time.sleep(1)


def _materially_different(new: dict | None, old: dict | None) -> bool:
    """Timestamps are recomputed on every push, so rounding makes them drift a
    second either way even when nothing changed. Treat that as identical —
    otherwise each heartbeat re-pushes and visibly nudges the progress bar."""
    if new is None or old is None:
        return new is not old

    keys = set(new) | set(old)
    for key in keys - {"start", "end"}:
        if new.get(key) != old.get(key):
            return True

    for key in ("start", "end"):
        a, b = new.get(key), old.get(key)
        if (a is None) != (b is None):
            return True
        if a is not None and abs(a - b) > 2:
            return True

    return False


def push_with_fallback(rpc: Presence, payload: dict) -> dict:
    """A rejected keyword is a payload problem, not a dead socket. Drop the
    offending field and retry rather than tearing down a live connection.

    Optional keys are listed worst-first: activity_type and the artwork fields
    are cosmetic, so shedding them still leaves usable presence."""
    optional = ("details_url", "state_url", "large_url",
                "status_display_type", "activity_type", "large_text",
                "large_image", "end", "start")
    attempt = dict(payload)

    for _ in range(len(optional) + 1):
        try:
            rpc.update(**attempt)
            return attempt
        except (TypeError, AttributeError) as exc:
            # Match the quoted name Python puts in "unexpected keyword argument
            # 'x'". A bare substring test is not safe here: 'state' is a
            # substring of 'state_url', so an error about state_url would shed
            # the artist line instead and leave the real offender in place.
            text = str(exc)
            dropped = next((k for k in attempt if f"'{k}'" in text), None)
            if dropped is None:
                # Otherwise shed the least important field still present.
                dropped = next((k for k in optional if k in attempt), None)
            if dropped is None:
                raise
            attempt.pop(dropped)
            print(f"[rpc] dropping '{dropped}' ({exc})")

    return attempt


# Discord requires `details` and `state` to be at least two characters and
# rejects the whole activity otherwise — which surfaces as a server error, not
# as a validation warning. One-character song titles exist ("4", "T", "Ø"), so
# this is reachable in normal listening. U+2060 WORD JOINER is invisible when
# rendered but counts toward the length.
_DISCORD_MIN_FIELD = 2
_INVISIBLE_PAD = "⁠"


def pad_for_discord(text: str, label: str) -> str:
    if not text.strip():
        return "Unknown"
    if len(text) >= _DISCORD_MIN_FIELD:
        return text
    print(f"[rpc] padded {label} {text!r} — Discord requires 2+ characters")
    return text + _INVISIBLE_PAD * (_DISCORD_MIN_FIELD - len(text))


class Playhead:
    """Holds `start` steady for the length of a track.

    Discord renders the position as `now - start`, so `start` is an anchor, not
    a reading. Recomputing it on every push is what makes the bar twitch: the
    anchor moves, and the bar jumps with it.

    The anchor is derived from the phone's `elapsed`, which can be stale —
    `currentPlaybackTime` is not updated eagerly for a backgrounded app. That
    staleness is not symmetric. A late read makes the song look *earlier* than
    it really is and can never make it look later, so of two candidate anchors
    the smaller `start` is always the less stale one. Keeping the minimum
    therefore converges on the truth instead of wandering around it.

    Three cases break that rule and are handled explicitly:

    * **A different track** — nothing worth keeping.
    * **A seek** — a deliberate jump in either direction, told apart from
      staleness by size.
    * **The seconds just after a track change** — `currentPlaybackTime` can
      still be reporting the previous song, which reads as *further along* and
      would otherwise be locked in as a great anchor. The correction push
      2.5s later must be able to move it back, so during the settle window
      every reading is accepted.
    """

    def __init__(self) -> None:
        self.key: str | None = None
        self.start: float | None = None
        self.settled_at = 0.0

    def reset(self) -> None:
        """Playback stopped. A pause of unknown length invalidates the anchor —
        Discord would otherwise keep ticking through it."""
        self.key = None
        self.start = None

    def anchor(self, key: str, elapsed: float, now: float) -> float:
        candidate = now - elapsed

        if key != self.key or self.start is None:
            self.key = key
            self.start = candidate
            self.settled_at = now + PLAYHEAD_SETTLE_WINDOW
            return self.start

        drift = candidate - self.start

        if now < self.settled_at:
            self.start = candidate
        elif abs(drift) > PLAYHEAD_SEEK_TOLERANCE:
            print(f"[playhead] re-anchored {drift:+.1f}s — treating as a seek")
            self.start = candidate
        elif drift < -0.5:
            # Implies the song is further along than the current anchor says.
            # Staleness cannot produce that, so this reading is the fresher one.
            print(f"[playhead] corrected {-drift:.1f}s forward")
            self.start = candidate

        return self.start


playhead = Playhead()

_unresolved_seen: set[str] = set()


def build_payload(track: dict, observed_at: float) -> dict:
    """`observed_at` is when this reading *arrived from the phone*, not now.

    That distinction is the whole ballgame for the progress bar. This function
    runs once a second, but the phone only pushes every thirty, so `track` holds
    the same frozen `elapsed` for thirty consecutive calls. Pairing that frozen
    reading with a live `time.time()` makes the computed `start` slide forward a
    second per second — the bar falls behind, then snaps back when the next push
    lands. Pairing it with the arrival time instead keeps `start` genuinely
    constant between pushes, which is what Discord needs to tick smoothly."""
    title = str(track.get("title", "Unknown Track"))[:128]
    artist = str(track.get("artist", "Unknown Artist"))[:128]
    album = str(track.get("album", ""))[:128]
    track_key = f"{artist}|{title}|{album}"

    payload: dict = {
        "details": pad_for_discord(title, "title"),
        "state": pad_for_discord(artist, "artist"),
    }

    # Renders as "Listening to <app name>" instead of "Playing".
    if LISTENING is not None:
        payload["activity_type"] = LISTENING

    display = _DISPLAY_CHOICES.get(STATUS_LINE)
    if display is not None:
        payload["status_display_type"] = display

    duration = float(track.get("duration") or 0)
    elapsed = float(track.get("elapsed") or 0)
    if duration > 0:
        start = playhead.anchor(track_key, elapsed, observed_at)
        payload["start"] = int(start)
        payload["end"] = int(start + duration)

    # Preference order, best first:
    #   1. catalog ID the phone supplied  — exact, no guessing
    #   2. artwork the phone uploaded     — exact, but only for local files
    #   3. fuzzy search by title/artist   — best effort
    art, matched_album = None, ""

    store_id = str(track.get("store_id") or "").strip()
    if store_id and store_id not in ("0", "-1"):
        art, matched_album = artwork_by_store_id(store_id)

    if not art and track.get("artwork_b64"):
        art = store_uploaded_artwork(track_key, track["artwork_b64"])
        if art:
            matched_album = album

    # Heartbeats arrive without the JPEG; reuse the one already on disk.
    if not art:
        art = existing_uploaded_artwork(track_key)
        if art:
            matched_album = album

    if not art:
        art, matched_album = artwork_url(title, artist, album)

    # Clickable text and artwork. Discord opens details_url from the title line,
    # state_url from the artist line and large_url from the cover. Only ever
    # sourced from the exact store-ID lookup, never from fuzzy matching.
    links = catalog_links(store_id) if store_id else {}
    if links.get("song"):
        payload["details_url"] = links["song"][:256]
    if links.get("artist"):
        payload["state_url"] = links["artist"][:256]

    if art:
        payload["large_image"] = art
        # A link on the cover only makes sense when there's a cover to click.
        if links.get("album"):
            payload["large_url"] = links["album"][:256]
        if SHOW_ALBUM:
            # Prefer the album the artwork actually came from — if the match was
            # imperfect, hovering the cover reveals what it thinks it found.
            label = matched_album or album
            if label:
                payload["large_text"] = label
    else:
        # Record what the phone actually sent, once per track. Every artwork
        # path failing silently is what made this bug expensive to find.
        seen_key = f"{artist}|{title}|{album}"
        if seen_key not in _unresolved_seen:
            _unresolved_seen.add(seen_key)
            print(f"[art] UNRESOLVED {title} — {artist} [{album}] "
                  f"store_id={store_id or 'none'} "
                  f"uploaded_jpeg={'yes' if track.get('artwork_b64') else 'no'}")

    return payload


class QuietHTTPServer(ThreadingHTTPServer):
    """A dropped connection is not an error worth a stack trace.

    cloudflared severs its connection whenever the tunnel restarts, and the
    default handler answers that with a fifteen-line traceback — which, sitting
    at the bottom of relay.log, reads exactly like a crash. Real errors still
    get the full trace."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            print("[http] client disconnected mid-request (normal on tunnel restart)")
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, body: str = "") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/now-playing":
            return self._reply(404, "not found")

        # Constant-time-ish check; the tunnel already gives you TLS.
        if not SECRET or self.headers.get("X-Relay-Secret", "") != SECRET:
            return self._reply(401, "unauthorized")

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._reply(400, "bad json")

        _, previous = state.get()
        state.set(body if body.get("playing") else None)
        note_checkin(previous)
        self._reply(204)

    def do_GET(self) -> None:
        path = self.path.rstrip("/")

        if path == "/health":
            # Body stays exactly "ok" — Shortcuts and the earlier setup notes
            # test for that string. Version lives on its own route instead.
            return self._reply(200, "ok")

        if path == "/version":
            return self._reply(200, RELAY_VERSION)

        # Lets a Shortcut check whether the phone is still reporting before it
        # bothers launching the app — iOS has no way to ask that locally, but
        # the relay knows, because the app checks in every 30 seconds.
        # Discord's CDN fetches this itself and can't send our header, so it
        # stays open. Filenames are hashes, so they aren't enumerable.
        if path.startswith("/art/"):
            name = os.path.basename(path[len("/art/"):])
            target = (ART_DIR / name).resolve()
            if (target.parent != ART_DIR or not target.is_file()
                    or not name.endswith(".jpg")):
                return self._reply(404, "not found")
            blob = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            self.wfile.write(blob)
            return

        if path == "/status":
            if not SECRET or self.headers.get("X-Relay-Secret", "") != SECRET:
                return self._reply(401, "unauthorized")
            _, updated_at = state.get()
            fresh = updated_at > 0 and (time.time() - updated_at) < IDLE_TIMEOUT
            return self._reply(200, "alive" if fresh else "stale")

        self._reply(404, "not found")

    def log_message(self, *args) -> None:
        pass  # the RPC worker already logs anything interesting


def _ensure_output() -> None:
    """Under pythonw.exe there is no console and sys.stdout is None, so any
    print() raises. Send output to a log file next to the script instead."""
    if sys.stdout is not None:
        return
    log = pathlib.Path(__file__).resolve().parent / "relay.log"
    handle = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = handle
    sys.stderr = handle
    print(f"\n=== started {time.strftime('%Y-%m-%dT%H:%M:%S')} ===")


def main() -> None:
    _ensure_output()

    # Before anything that can fail, so a misconfigured relay still reports
    # which copy it is.
    if "--version" in sys.argv:
        print(RELAY_VERSION)
        return

    if "--summary" in sys.argv:
        print_summary()
        return

    if not CLIENT_ID:
        raise SystemExit("Set DISCORD_CLIENT_ID (from discord.com/developers).")
    if not SECRET:
        raise SystemExit("Set RELAY_SECRET to a long random string.")

    print(f"[init] relay {RELAY_VERSION}")

    try:
        from importlib.metadata import version
        print(f"[init] pypresence {version('pypresence')}")
    except Exception:
        pass

    _log_line("relay started")
    threading.Thread(target=rpc_worker, daemon=True).start()
    server = QuietHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[http] listening on 127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
