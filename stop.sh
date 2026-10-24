#!/usr/bin/env bash
# Stop a background ATLAS started with ./run.sh --bg (or any ./run.sh instance).
cd "$(dirname "$0")"
if [ -f atlas.pid ] && kill -0 "$(cat atlas.pid)" 2>/dev/null; then
  kill "$(cat atlas.pid)" && echo "ATLAS stopped."
  rm -f atlas.pid
else
  echo "ATLAS is not running (no live pid file)."; rm -f atlas.pid
fi
