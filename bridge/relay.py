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

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pypresence import Presence

try:
    # Added in pypresence 4.3.0. update() calls .value on this, so a plain
    # int raises AttributeError rather than being coerced.
    from pypresence import ActivityType
    LISTENING = ActivityType.LISTENING
except ImportError:
    LISTENING = None

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
SECRET = os.environ.get("RELAY_SECRET", "").strip()
PORT = int(os.environ.get("RELAY_PORT", "8787"))

# Clear presence if the phone stops reporting — covers force-quit and the app
# getting evicted in the background.
IDLE_TIMEOUT = 90.0

# Discord rate limits activity updates. Coalesce rapid changes (skipping
# through a playlist) rather than firing one call per skip.
MIN_PUSH_GAP = 3.0


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


def artwork_url(title: str, artist: str) -> str | None:
    """Public iTunes Search lookup. Current Discord builds accept a plain
    https URL for large_image; older ones need a registered asset key."""
    key = f"{artist}|{title}"
    if key in _artwork_cache:
        return _artwork_cache[key] or None

    query = urllib.parse.urlencode(
        {"term": f"{artist} {title}", "entity": "song", "limit": 1}
    )
    try:
        with urllib.request.urlopen(
            f"https://itunes.apple.com/search?{query}", timeout=6
        ) as resp:
            payload = json.load(resp)
        results = payload.get("results") or []
        art = results[0].get("artworkUrl100", "") if results else ""
        art = art.replace("100x100bb", "512x512bb")
    except Exception:
        art = ""

    _artwork_cache[key] = art
    return art or None


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
        if track and time.time() - updated_at > IDLE_TIMEOUT:
            track = None

        payload = build_payload(track) if track else None

        should_push = payload != last_payload and (
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
            except Exception as exc:
                print(f"[rpc] lost connection ({exc})")
                try:
                    rpc.close()
                except Exception:
                    pass
                rpc = None
                continue

        time.sleep(1)


def push_with_fallback(rpc: Presence, payload: dict) -> dict:
    """A rejected keyword is a payload problem, not a dead socket. Drop the
    offending field and retry rather than tearing down a live connection.

    Optional keys are listed worst-first: activity_type and the artwork fields
    are cosmetic, so shedding them still leaves usable presence."""
    optional = ("activity_type", "large_text", "large_image", "end", "start")
    attempt = dict(payload)

    for _ in range(len(optional) + 1):
        try:
            rpc.update(**attempt)
            return attempt
        except (TypeError, AttributeError) as exc:
            # Prefer a key the error names; otherwise shed the least important
            # field still present.
            dropped = next((k for k in attempt if k in str(exc)), None)
            if dropped is None:
                dropped = next((k for k in optional if k in attempt), None)
            if dropped is None:
                raise
            attempt.pop(dropped)
            print(f"[rpc] dropping '{dropped}' ({exc})")

    return attempt


def build_payload(track: dict) -> dict:
    title = str(track.get("title", "Unknown Track"))[:128]
    artist = str(track.get("artist", "Unknown Artist"))[:128]
    album = str(track.get("album", ""))[:128]

    payload: dict = {
        "details": title,
        "state": artist,
    }

    # Renders as "Listening to <app name>" instead of "Playing".
    if LISTENING is not None:
        payload["activity_type"] = LISTENING

    duration = float(track.get("duration") or 0)
    elapsed = float(track.get("elapsed") or 0)
    if duration > 0:
        start = time.time() - elapsed
        payload["start"] = int(start)
        payload["end"] = int(start + duration)

    art = artwork_url(title, artist)
    if art:
        payload["large_image"] = art
        if album:
            payload["large_text"] = album

    return payload


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

        state.set(body if body.get("playing") else None)
        self._reply(204)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            return self._reply(200, "ok")
        self._reply(404, "not found")

    def log_message(self, *args) -> None:
        pass  # the RPC worker already logs anything interesting


def main() -> None:
    if not CLIENT_ID:
        raise SystemExit("Set DISCORD_CLIENT_ID (from discord.com/developers).")
    if not SECRET:
        raise SystemExit("Set RELAY_SECRET to a long random string.")

    try:
        from importlib.metadata import version
        print(f"[init] pypresence {version('pypresence')}")
    except Exception:
        pass

    threading.Thread(target=rpc_worker, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[http] listening on 127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
