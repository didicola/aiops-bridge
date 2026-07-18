# Dockerfile.telegram — MINIMAL image that packages ONLY the Telegram bridge.
#
# Goal: run scripts/asi-telegram-bridge.py as a standalone cloud container so the
# Telegram bot survives the LOCAL machine dying. The container polls Telegram and
# routes LLM calls to a CLOUD blind-proxy public URL (NOT localhost) — see below.
#
# Base: python:3.12-slim (small, has python3 + stdlib urllib — no pip needed).
FROM python:3.12-slim

# The bridge uses ONLY the Python standard library (urllib), so there is NO
# third-party dependency and NO PyPI reachability requirement during build.
# This keeps the cloud build bulletproof on any CI.

# Copy the bridge script into the image. We COPY it (never edit it) so the
# fortress-immutable contract holds.
WORKDIR /app
COPY asi-telegram-bridge.py /app/asi-telegram-bridge.py

# ── Cloud egress configuration ───────────────────────────────────────────────
# IMPORTANT: the bridge reads BLIND_PROXY_URL from env and forwards LLM calls
# there. In the cloud that URL MUST be the PUBLIC HF Spaces blind-proxy URL
# (e.g. https://<space>.hf.space/v1/chat/completions), NOT localhost. Pass it at
# runtime via Space secrets / env vars — do NOT hardcode localhost here.
#
# Tor: the bridge routes api.telegram.org through Tor when TOR_SOCKS is set.
# The cloud container has NO local Tor, so we UNSET TOR_SOCKS (empty) which the
# bridge interprets as "direct egress" (no proxy). This is the safe cloud default.
ENV TOR_SOCKS=""
ENV BLIND_PROXY_URL=""
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""

# A poller does not need an exposed port, but documenting intent is harmless.
# EXPOSE not required for a getUpdates poller.

# Run the bridge. It loops forever, polling Telegram and replying. Missing
# TELEGRAM_BOT_TOKEN makes it print NEEDS and exit 0 (fail-safe, never fakes).
ENTRYPOINT ["python3", "/app/asi-telegram-bridge.py"]
