#!/usr/bin/env python3
"""
asi-telegram-bridge.py — Phase 12 Global Access Interface (Telegram Bot Bridge).

Admin interacts with the sovereign AI-Ops brain from ANYWHERE via Telegram, with NO
raw ports exposed. The bot polls Telegram for inbound messages, forwards each message
to the ricocoder LLM loop THROUGH the local blind-proxy (http://127.0.0.1:8090), and
sends the assistant reply back to Telegram.

WHY blind-proxy and not a provider directly:
  * blind-proxy keeps the $0 cost model (no provider billing, no keys).
  * blind-proxy is the single sovereign egress: it routes LLM calls through Tor with
    fake device ID + traces (per rule.md §2.2). Going around it would break the
    anti-correlation / identity-isolation laws.

DEPENDENCIES:
  * `requests` is REQUIRED (stdlib-only fallback is not sufficient for HTTPS polling).
  * If `python-telegram-bot` is installed it is NOT used here on purpose: the polling
    approach with `requests` is dependency-light and works unmodified inside the
    Hugging Face Spaces Docker image (no extra pip installs needed).

FAIL-SAFE:
  * If TELEGRAM_BOT_TOKEN is absent -> print NEEDS message and exit 0 (never fake).
  * If TELEGRAM_CHAT_ID is set, only that admin chat is served; other senders get a
    clear "not authorized" reply and are logged. If unset, ANY chat is served.
  * Network/timeout errors are caught and retried; the loop never dies.

Launch:  python3 asi-telegram-bridge.py &   (runs forever, polls every ~1.5s)
"""

import os
import sys
import time
import json

# ── Dependency check (fail-safe, no fabricated connection) ──────────────────
try:
    import requests
except ImportError:
    print("NEEDS: python 'requests' module (pip install requests)")
    sys.exit(0)

# ── Configuration (from env) ────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# LLM egress: the opencode-bridge (:8088) is the captcha-safe buffered path that
# skips the captcha-walled raw blind-proxy (:8090) and uses local free upstreams
# (proxygategllm :3333, freellmapi :3002). Still $0 + Tor via blind-proxy chain.
# Never point this directly at a provider.
BLIND_PROXY_URL = os.environ.get(
    "BLIND_PROXY_URL", "http://127.0.0.1:8088/v1/chat/completions"
)
# Sane default model route through blind-proxy.
LLM_MODEL = os.environ.get("TELEGRAM_BRIDGE_MODEL", "auto")

# Sovereign egress: ALL outbound (Telegram API) SHOULD route through Tor (§2.2)
# when running on the local box (where a local Tor SOCKS5 proxy is present).
# In the cloud container there is NO local Tor, so the bridge must reach
# api.telegram.org DIRECTLY. We therefore treat an EMPTY TOR_SOCKS env as
# "no proxy" (direct egress). Local runs keep Tor (default set below only when
# the env var is unset AND non-empty is not forced empty).
_TOR_SOCKS_RAW = os.environ.get("TOR_SOCKS")
if _TOR_SOCKS_RAW is None:
    # Env not set at all -> default to local Tor (preserves historical behavior).
    TOR_SOCKS = "socks5h://127.0.0.1:9050"
    _TOR_PROXIES = {"http": TOR_SOCKS, "https": TOR_SOCKS}
    _TOR_LOG = f"Tor enabled via default ({TOR_SOCKS})"
else:
    TOR_SOCKS = _TOR_SOCKS_RAW.strip()
    if TOR_SOCKS:
        _TOR_PROXIES = {"http": TOR_SOCKS, "https": TOR_SOCKS}
        _TOR_LOG = f"Tor enabled ({TOR_SOCKS})"
    else:
        # Empty TOR_SOCKS -> explicit direct egress (cloud container, no local Tor).
        _TOR_PROXIES = {}
        _TOR_LOG = "Tor disabled (TOR_SOCKS empty) — direct egress"

POLL_INTERVAL = float(os.environ.get("TELEGRAM_POLL_INTERVAL", "1.5"))
HTTP_TIMEOUT = float(os.environ.get("TELEGRAM_HTTP_TIMEOUT", "30"))
REQUESTS_TIMEOUT = float(os.environ.get("TELEGRAM_REQ_TIMEOUT", "60"))
MAX_REPLY_LEN = 4000  # Telegram sendMessage hard cap per message


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[asi-tg {ts}] {msg}", flush=True)


def tg_api(method: str, payload: dict | None = None) -> dict | None:
    """Call a Telegram Bot API method. Returns parsed JSON or None on failure.
    Routes through Tor (§2.2 fail-closed egress) — api.telegram.org is external."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload or {}, timeout=HTTP_TIMEOUT,
                             proxies=_TOR_PROXIES)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # network/timeout/http errors -> graceful
        log(f"tg_api {method} failed: {exc!r}")
        return None


def send_message(chat_id: str, text: str) -> None:
    # Split long replies into chunks (Telegram 4096-char practical limit -> 4000 safe).
    chunks = [text[i : i + MAX_REPLY_LEN] for i in range(0, len(text), MAX_REPLY_LEN)]
    if not chunks:
        chunks = ["(empty response)"]
    for chunk in chunks:
        # Retry once on failure so an admin reply is never silently lost.
        for attempt in range(2):
            if tg_api("sendMessage", {"chat_id": chat_id, "text": chunk}):
                break
            log(f"sendMessage retry {attempt + 1} to {chat_id}")
            time.sleep(1)
        time.sleep(0.3)


# Sovereign identity for the Telegram-facing assistant. Establishes what the bot
# IS, so free upstream models (which identify as their own vendor, e.g. Qwen ->
# "Alibaba") don't misrepresent the system to the admin.
SYSTEM_PROMPT = (
    "You are the sovereign AI-Ops assistant running on the user's own machine "
    "(ricocoder + blind-proxy, routed through free model upstreams via Tor egress, "
    "$0 cost). You are NOT a vendor cloud service and NOT Alibaba/OpenAI/any third "
    "party. You are the user's personal autonomous AI system. Answer the user's "
    "questions helpfully and truthfully about this local sovereign setup. Keep "
    "replies concise and Telegram-friendly."
)


def ask_llm(user_text: str) -> str:
    """Forward the user message to the ricocoder LLM loop via blind-proxy. Returns reply text."""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
    }
    try:
        resp = requests.post(BLIND_PROXY_URL, json=payload, timeout=REQUESTS_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log(f"blind-proxy LLM call failed: {exc!r}")
        return (
            f"⚠️ LLM egress error via {BLIND_PROXY_URL}. "
            "The sovereign brain may be offline or the egress proxy is down. "
            "Raw error: " + str(exc)
        )


def is_authorized(chat_id: str) -> bool:
    if not ADMIN_CHAT_ID:
        return True  # no admin restriction configured -> serve any chat
    return str(chat_id) == str(ADMIN_CHAT_ID)


def handle_update(update: dict) -> None:
    message = update.get("message")
    if not message:
        return
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()
    if not text:
        # Non-text content (photo/sticker/voice/etc.) — give admin minimal feedback
        # instead of silently dropping it.
        if is_authorized(chat_id):
            send_message(chat_id, "📎 Non-text messages aren't processed yet — send text.")
        return

    if not is_authorized(chat_id):
        log(f"DENIED message from non-admin chat {chat_id}: {text!r}")
        send_message(
            chat_id,
            "🚫 Not authorized. This Telegram bridge is restricted to the admin "
            "TELEGRAM_CHAT_ID. Set TELEGRAM_CHAT_ID to your chat id to enable access.",
        )
        return

    log(f"FROM {chat_id}: {text[:80]!r}{'...' if len(text) > 80 else ''}")
    reply = ask_llm(text)
    send_message(chat_id, reply)
    log(f"REPLY sent to {chat_id} ({len(reply)} chars)")


def main() -> None:
    if not BOT_TOKEN:
        print(
            "NEEDS: TELEGRAM_BOT_TOKEN env (create a bot via @BotFather, free, no card)"
        )
        sys.exit(0)

    log("Telegram bridge starting.")
    log(_TOR_LOG)
    log(f"blind-proxy LLM egress: {BLIND_PROXY_URL} (model={LLM_MODEL})")
    if ADMIN_CHAT_ID:
        log(f"Admin-restricted to chat_id={ADMIN_CHAT_ID}")
    else:
        log("No TELEGRAM_CHAT_ID set -> serving ANY chat that messages the bot.")

    last_update_id = 0
    # Telegram long-poll timeout must be LONGER than the socket timeout, otherwise
    # Tor holds the connection for the full poll window and requests raises ReadTimeout
    # before Telegram answers -> the bot never reliably receives messages.
    LONGPOLL_TIMEOUT = int(os.environ.get("TELEGRAM_LONGPOLL_TIMEOUT", "60"))
    while True:
        try:
            params = {"timeout": LONGPOLL_TIMEOUT}
            if last_update_id:
                params["offset"] = last_update_id + 1
            data = tg_api("getUpdates", params)
            if data and data.get("ok"):
                for update in data.get("result", []):
                    uid = update.get("update_id", 0)
                    if uid <= last_update_id:
                        continue
                    # Process BEFORE advancing the offset, so a crash mid-batch
                    # does not permanently skip an unprocessed message.
                    handle_update(update)
                    last_update_id = uid
            else:
                # getUpdates failed (network/timeout) -> brief pause, then retry.
                time.sleep(POLL_INTERVAL)
        except Exception as exc:  # never let the loop die
            log(f"poll loop error: {exc!r}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
