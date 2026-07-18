#!/usr/bin/env python3
"""Cloud Sentinel (Phase 16 port of scripts/autonomous-log-healer.sh).

The local log-healer tailed *this host's* systemd journals — impossible from an
ephemeral cloud runner. So the cloud incarnation instead health-checks the LIVE
cloud components (the brain proxy, the D1 gateway, the Telegram bridge) and, on
failure, records the incident to shared memory and alerts the admin over Telegram.

Env:
  BRAIN_URL / PROXY_AUTH_TOKEN      (from asi_cloud)
  TELEGRAM_BRIDGE_URL               optional: the bridge Worker /?selftest=1 URL
  TELEGRAM_BOT_TOKEN + TELEGRAM_ADMIN_CHAT   optional: direct alert channel
"""
import json
import os
import time
import urllib.parse
import urllib.request

import asi_cloud as asi

BRIDGE_URL = os.environ.get("TELEGRAM_BRIDGE_URL", "").rstrip("/")
BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_ADMIN_CHAT", "")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"


def _tg(method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT}/{method}",
        data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def heal_webhook():
    """Self-heal: if Telegram's webhook is unset or erroring, re-point it at the
    cloud bridge Worker. Runs from GitHub Actions, so it repairs the live bot even
    with the local host powered OFF (the core machine-jumper-matrix guarantee)."""
    if not (BOT and BRIDGE_URL):
        return "skip (no BOT/BRIDGE_URL)"
    try:
        info = _tg("getWebhookInfo", {}).get("result", {})
    except Exception as e:  # noqa: BLE001
        return f"getWebhookInfo failed: {e}"
    cur = (info.get("url") or "").rstrip("/")
    want = BRIDGE_URL.rstrip("/")
    last_err = info.get("last_error_message")
    needs = (cur != want) or bool(last_err)
    if not needs:
        return f"healthy (url set, no errors, pending={info.get('pending_update_count')})"
    try:
        res = _tg("setWebhook", {"url": want, "max_connections": 40,
                                 "drop_pending_updates": "false"})
    except Exception as e:  # noqa: BLE001
        return f"setWebhook failed: {e}"
    ok = res.get("ok")
    msg = f"REPAIRED webhook (was url={cur or 'NONE'} err={last_err}) -> {want} ok={ok}"
    alert(msg)
    return msg


def alert(msg):
    print("ALERT:", msg)
    if not (BOT and CHAT):
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": CHAT, "text": f"[cloud-sentinel] {msg}"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data=data, headers={"User-Agent": UA})
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:  # noqa: BLE001
        print("telegram alert failed:", e)


def check(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        dt = round(time.time() - t0, 2)
        print(f"[OK]   {name} ({dt}s) {detail}")
        return True, detail
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {e}")
        return False, str(e)


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")[:200]


def main():
    results = {}

    ok, d = check("brain-proxy LLM", lambda: asi.ask("Reply: PONG", max_tokens=8))
    results["brain"] = ok
    ok2, d2 = check("d1 gateway", lambda: asi.d1_query("SELECT COUNT(*) AS n FROM directives"))
    results["d1"] = ok2

    if BRIDGE_URL:
        ok3, d3 = check("telegram bridge", lambda: _get(f"{BRIDGE_URL}/?selftest=1"))
        results["bridge"] = ok3

    # Self-heal the Telegram webhook (works with the local host OFF).
    if BOT and BRIDGE_URL:
        wh = heal_webhook()
        print("webhook:", wh)
        results["webhook"] = wh.startswith(("healthy", "REPAIRED"))

    healthy = sum(1 for v in results.values() if v)
    total = len(results)
    summary = f"{healthy}/{total} cloud components healthy: " + \
        ", ".join(f"{k}={'up' if v else 'DOWN'}" for k, v in results.items())
    print(summary)

    asi.log_run("cloud-sentinel", "ok" if healthy == total else "degraded", summary)

    if healthy < total:
        down = [k for k, v in results.items() if not v]
        alert(f"DEGRADED — down: {', '.join(down)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
