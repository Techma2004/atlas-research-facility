#!/usr/bin/env bash
# One-command launcher (uses uv): creates ./.venv from uv.lock, then starts ATLAS on localhost.
# Never uses sudo, never touches system services, never overwrites your configuration.
#   ./run.sh            start in the foreground (Ctrl+C to stop)
#   ./run.sh --bg       start in the background (stop with ./stop.sh)
#   ./run.sh --onion    also install the optional Tor/SOCKS dependency (extra: onion)
set -euo pipefail
cd "$(dirname "$0")"

BG=0; EXTRAS=()
for a in "$@"; do case "$a" in
  --bg) BG=1 ;;
  --onion) EXTRAS+=(--extra onion) ;;
  -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
  *) echo "Unknown option: $a"; exit 2 ;;
esac; done

if ! command -v uv >/dev/null 2>&1; then
  cat <<'MSG'
uv is required but was not found. Install it yourself (no sudo needed), then re-run ./run.sh:
  curl -LsSf https://astral.sh/uv/install.sh | sh        # official installer, installs to ~/.local/bin
  # or:  pipx install uv      /     pip install --user uv
MSG
  exit 1
fi

# Creates/updates ./.venv (Python >= 3.10, downloaded by uv only if none is installed) from uv.lock.
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --frozen --no-dev "${EXTRAS[@]}" --quiet

# Load .env without overriding variables already exported in the shell.
if [ -f .env ]; then
  while IFS='=' read -r k v; do
    case "$k" in ''|\#*) continue ;; esac
    k="$(echo "$k" | tr -d '[:space:]')"
    if [ -z "${!k+x}" ]; then export "$k=${v%\"}"; fi
  done < .env
fi

HOST="${ATLAS_HOST:-127.0.0.1}"; PORT="${ATLAS_PORT:-8765}"
if [ -f atlas.pid ] && kill -0 "$(cat atlas.pid)" 2>/dev/null; then
  echo "ATLAS is already running (pid $(cat atlas.pid)) at http://$HOST:$PORT"; exit 0
fi

echo "ATLAS dashboard:  http://$HOST:$PORT"
PY=.venv/bin/python
if [ "$BG" = 1 ]; then
  nohup "$PY" -m atlas >atlas.log 2>&1 &
  echo $! > atlas.pid
  sleep 1.5
  if kill -0 "$(cat atlas.pid)" 2>/dev/null; then echo "Running in background (pid $(cat atlas.pid)). Stop with ./stop.sh   Logs: atlas.log"
  else echo "Failed to start - see atlas.log"; rm -f atlas.pid; exit 1; fi
else
  echo "Stop with Ctrl+C (or ./stop.sh from another terminal)."
  "$PY" -m atlas &
  echo $! > atlas.pid
  trap 'kill "$(cat atlas.pid)" 2>/dev/null || true; rm -f atlas.pid' INT TERM EXIT
  wait
fi
