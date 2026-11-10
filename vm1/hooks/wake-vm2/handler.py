"""Hermes gateway hook: when a message starts an agent turn, make sure VM 2 (freellmapi + DSH) is awake.

VM 2 idle-pauses on InstaBox while VM 1 keeps running. A paused VM can't be reached over Tailscale, so the
model calls would fail. On `agent:start` we ping freellmapi (2 s). Only if it doesn't answer do we ask the
Cloudflare worker to wake VM 2 (the worker holds the InstaBox key) and wait until freellmapi answers, up to
~60 s, so this turn's model call succeeds. Wake-on-demand only: nothing runs without a real message.
"""
import asyncio
import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("hooks.wake-vm2")
FREELLMAPI_PING = os.environ.get("ATLAS_FREELLMAPI_PING", "http://VM2_TAILSCALE_IP:3001/api/ping")
WORKER_CONF = "/data/pa/worker.json"


def _ok(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _ensure_vm2_awake() -> str:
    if _ok(FREELLMAPI_PING, 2):
        return "awake"
    try:
        conf = json.load(open(WORKER_CONF))
        req = urllib.request.Request(conf["url"].rstrip("/") + "/wake/vm2", method="POST", data=b"{}",
                                     headers={"Authorization": f"Bearer {conf['secret']}", "Content-Type": "application/json",
                                              "User-Agent": "atlas-wake-hook/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            res = r.read(300).decode(errors="replace")
    except Exception as e:
        return f"wake request failed: {type(e).__name__}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if _ok(FREELLMAPI_PING, 3):
            return f"woken ({res[:120]})"
        time.sleep(3)
    return f"wake requested but freellmapi still down after 60 s ({res[:120]})"


async def handle(event_type: str, context: dict):
    t = time.time()
    result = await asyncio.to_thread(_ensure_vm2_awake)
    if result != "awake":
        log.warning("wake-vm2: %s in %.1fs", result, time.time() - t)
