#!/usr/bin/env bash
# start_hf.sh — Entrypoint for the Hugging Face Spaces cloud clone.
#
# Launches, in order, inside the HF Space container:
#   1. blind-proxy          (node scripts/blind-proxy.js)          -> 0.0.0.0:8090
#   2. ai-dashboard         (python3 scripts/dashboard.py via uvicorn) -> 0.0.0.0:8080
#   3. the 5 autonomous agents (meta-orchestrator, model-discovery, log-healer,
#      dream-engine, evolutionary-engine) via the docker-compose stack if a Docker
#      daemon is present; otherwise they are launched directly as background processes.
#
# HF Spaces routes the container's :8080 and :8090 to the public Space URL, so we bind 0.0.0.0
# (this is the cloud instance, not the local sovereign box; 0.0.0.0 is correct here).
#
# HONEST NOTE: This script only starts processes. It does NOT open host firewall ports and does
# NOT touch any fortress-immutable files (blind-proxy.js / blind-proxy-lib.js are read-only copies).
set -uo pipefail

export DEBIAN_FRONTEND=noninteractive
export NODE_ENV=production
export BLIND_PROXY_PORT="${BLIND_PROXY_PORT:-8090}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
export BLIND_PROXY_URL="http://127.0.0.1:${BLIND_PROXY_PORT}"

RICO_DIR=/opt/ricocoder
LOG_DIR=/tmp/hfspace-logs
mkdir -p "$LOG_DIR"
cd "$RICO_DIR" || { echo "[start_hf] FATAL: cannot cd to $RICO_DIR"; exit 1; }

log(){ echo "[start_hf $(date -u +%H:%M:%S)] $*"; }

# ── 0. Memory sync (§8.48, zero-credential Telegram alternative to GitHub) ──
# Before launching anything, pull the LATEST AI memory (rule.md +
# dynamic-models.json) from Telegram so the cloud clone boots with the same
# rules/registry as the sovereign box. This needs NO new credential: the only
# credential the system holds is TELEGRAM_BOT_TOKEN (already present). GitHub
# pull (sync_memory_github.sh) remains a SECONDARY fallback, only if a real
# GITHUB_TOKEN is set. Fail-safe: a missing token prints "NEEDS" and the
# container still boots; a bot transport error is logged but non-fatal here.
log "Step 0: syncing memory from Telegram (zero-credential, via Tor)"
SYNC_TG="$RICO_DIR/scripts/cloud/sync_memory_telegram.py"
if [ -f "$SYNC_TG" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  if python3 "$SYNC_TG" --pull >> "$LOG_DIR/memory-sync.log" 2>&1; then
    log "Memory pull from Telegram OK (see memory-sync.log)"
  else
    log "WARN: Telegram memory pull failed (non-fatal); continuing boot"
  fi
else
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    log "NEEDS: TELEGRAM_BOT_TOKEN env — skipping Telegram memory pull"
  fi
fi
# Secondary fallback: GitHub memory pull only if a real token is present.
if [ -n "${GITHUB_TOKEN:-}" ] && [ -f "$RICO_DIR/scripts/cloud/sync_memory_github.sh" ]; then
  log "Secondary: attempting GitHub memory pull (GITHUB_TOKEN present)"
  bash "$RICO_DIR/scripts/cloud/sync_memory_github.sh" "${MEMORY_GITHUB_REPO:-}" \
    >> "$LOG_DIR/memory-sync.log" 2>&1 || log "WARN: GitHub memory pull failed (non-fatal)"
fi

# ── 1. blind-proxy ──────────────────────────────────────────────────────
log "Starting blind-proxy on 0.0.0.0:${BLIND_PROXY_PORT}"
if [ -f "$RICO_DIR/blind-proxy/blind-proxy.js" ]; then
  PORT="$BLIND_PROXY_PORT" node "$RICO_DIR/blind-proxy/blind-proxy.js" \
    > "$LOG_DIR/blind-proxy.log" 2>&1 &
  BP_PID=$!
  log "blind-proxy launched (pid=$BP_PID)"
else
  log "WARN: blind-proxy.js not found at $RICO_DIR/blind-proxy/blind-proxy.js; skipping"
fi

# ── 2. ai-dashboard (uvicorn, served on 0.0.0.0:DASHBOARD_PORT) ──────────
log "Starting ai-dashboard on 0.0.0.0:${DASHBOARD_PORT}"
if [ -f "$RICO_DIR/scripts/dashboard.py" ]; then
  VENV_PY="$RICO_DIR/.venv/bin/python"
  PY_BIN="${VENV_PY:-python3}"
  PORT="$DASHBOARD_PORT" nohup "$PY_BIN" -u "$RICO_DIR/scripts/dashboard.py" \
    --host 0.0.0.0 --port "$DASHBOARD_PORT" \
    > "$LOG_DIR/dashboard.log" 2>&1 &
  DASH_PID=$!
  log "ai-dashboard launched (pid=$DASH_PID)"
else
  log "WARN: dashboard.py not found; starting a minimal HTTP placeholder on :${DASHBOARD_PORT}"
  nohup python3 -u -c \
    "import http.server,socketserver; socketserver.TCPServer(('0.0.0.0',${DASHBOARD_PORT})).serve_forever()" \
    > "$LOG_DIR/dashboard.log" 2>&1 &
fi

# ── 3. Autonomous agents ────────────────────────────────────────────────
# Prefer the docker-compose stack if a Docker daemon is available.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  log "Docker daemon detected — starting 5-agent stack via docker compose"
  ( cd "$RICO_DIR/scripts/cloud" && docker compose up -d ) \
    >> "$LOG_DIR/agents.log" 2>&1 || log "WARN: docker compose stack failed; launching agents directly"
fi

# Direct-launch fallback for the 5 agents (works without Docker, as background loops).
launch_agent() {
  local name="$1"; local script="$2"; local interp="$3"
  if [ -f "$script" ]; then
    log "Launching agent: $name ($script)"
    nohup "$interp" -u "$script" \
      > "$LOG_DIR/${name}.log" 2>&1 &
  else
    log "SKIP agent (missing): $name ($script)"
  fi
}
launch_agent meta-orchestrator    "$RICO_DIR/scripts/meta-orchestrator.py"        python3
launch_agent model-discovery      "$RICO_DIR/scripts/model-discovery/daemon.py"    python3
launch_agent log-healer           "$RICO_DIR/scripts/autonomous-log-healer.sh"     bash
launch_agent dream-engine         "$RICO_DIR/scripts/dream-engine.sh"              bash
launch_agent evolutionary-engine  "$RICO_DIR/scripts/evolutionary-engine/evolve.sh" bash

# ── 4. Telegram bridge (Phase 12, §8.44) ───────────────────────────────
# Global access interface: admin chats with the brain from anywhere via Telegram,
# with NO raw ports exposed. The bridge forwards messages to blind-proxy (127.0.0.1:8090)
# for $0 cost + Tor egress, then replies on Telegram.
# Fail-safe: if TELEGRAM_BOT_TOKEN is absent the script prints NEEDS and exits 0,
# so a missing token never crashes the container.
log "Starting Telegram bridge (asi-telegram-bridge.py)"
if [ -f "$RICO_DIR/scripts/asi-telegram-bridge.py" ]; then
  nohup python3 -u "$RICO_DIR/scripts/asi-telegram-bridge.py" \
    > "$LOG_DIR/telegram-bridge.log" 2>&1 &
  TG_PID=$!
  log "Telegram bridge launched (pid=$TG_PID)"
else
  log "WARN: asi-telegram-bridge.py not found; skipping Telegram bridge"
fi

# ── Keep-alive: tail logs so the container stdout shows diagnostics ─────
log "All services launched. Tailing logs (Ctrl-C / container stop ends this)."
log "Public access: HF Spaces routes :${DASHBOARD_PORT} (dashboard) and :${BLIND_PROXY_PORT} (blind-proxy)."
tail -F "$LOG_DIR"/*.log 2>/dev/null &
wait
