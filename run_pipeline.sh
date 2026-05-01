#!/usr/bin/env bash
# tech-radar nightly pipeline launcher (Linux / company Ubuntu).
#
# Counterpart of run_pipeline.bat for the company deployment. Differences:
#   - Assumes the LLM endpoint (vLLM or llama.cpp) is managed by systemd or
#     docker compose with `restart: unless-stopped`, so we don't try to start
#     it ourselves. We just poll until it's reachable, with a generous timeout.
#   - No PYTHONIOENCODING needed (Linux defaults to UTF-8).
#   - Slack/Notion env vars are intentionally left blank in this deployment;
#     the pipeline detects their absence and skips both, leaving paper content
#     local-only.
#
# Install via cron (example: every weekday at 23:30):
#   30 23 * * 1-5 /home/USER/tech-radar/run_pipeline.sh
#
# Or via systemd timer if you prefer; see docs/.

set -euo pipefail

# Resolve the script's own directory so cron can call us with any cwd.
WORKDIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LOGDIR="$WORKDIR/data/logs"
mkdir -p "$LOGDIR"

LOGFILE="$LOGDIR/pipeline_$(date +%Y%m%d).log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"; }

log "START (host=$(hostname))"

# --- Wait for the local LLM endpoint to be reachable -------------------------
# Read LLM_BASE_URL from .env if present (one line, simple key=value parse —
# we don't need full dotenv semantics for this single-purpose health check).
LLM_URL=""
if [ -f "$WORKDIR/.env" ]; then
  LLM_URL="$(awk -F= '/^LLM_BASE_URL=/{ sub(/^LLM_BASE_URL=/,""); gsub(/[\"\047 \r]/,""); print; exit }' "$WORKDIR/.env" || true)"
fi
LLM_URL="${LLM_URL:-http://localhost:8000/v1}"
HEALTH_URL="${LLM_URL%/}/models"

log "Waiting for LLM endpoint: $HEALTH_URL"
WAIT_SEC=0
WAIT_MAX=120
until curl -sf -o /dev/null --max-time 3 "$HEALTH_URL"; do
  sleep 5
  WAIT_SEC=$((WAIT_SEC + 5))
  if [ "$WAIT_SEC" -ge "$WAIT_MAX" ]; then
    log "LLM endpoint not ready after ${WAIT_MAX}s; aborting"
    exit 1
  fi
done
log "LLM endpoint ready after ${WAIT_SEC}s"

# --- Run the pipeline --------------------------------------------------------
cd "$WORKDIR"
log "Running pipeline..."
set +e
uv run tech-radar run --lookback-days 2 --verbose >> "$LOGFILE" 2>&1
EXIT_CODE=$?
set -e

log "END (exit=$EXIT_CODE)"
exit $EXIT_CODE
