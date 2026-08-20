#!/usr/bin/env python3
"""
Smallest possible Discord Rich Presence test. No HTTP, no tunnel, no phone —
just pypresence talking to the desktop client.

If this fails, the problem is Discord, pypresence, or your application ID.
If this works but relay.py doesn't, the problem is in relay.py.

    python bridge\\ipc_test.py YOUR_APPLICATION_ID
"""

import sys
import time

from pypresence import Presence

if len(sys.argv) < 2:
    raise SystemExit("Usage: python ipc_test.py YOUR_APPLICATION_ID")

client_id = sys.argv[1].strip()

try:
    from importlib.metadata import version
    print(f"pypresence {version('pypresence')}")
except Exception:
    pass

rpc = Presence(client_id)

print("connecting to Discord…")
rpc.connect()
print("connected")

# Deliberately minimal: no artwork, no timestamps, no activity_type.
rpc.update(details="Test Song", state="Test Artist")
print("presence pushed — check Discord now")

print("holding for 60s, Ctrl+C to stop")
try:
    time.sleep(60)
except KeyboardInterrupt:
    pass

rpc.clear()
rpc.close()
print("cleared")
