#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
SEARCH_PYTHON="${FARE_WATCH_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11}"
if [ ! -x "$SEARCH_PYTHON" ]; then SEARCH_PYTHON="python3"; fi
exec "$SEARCH_PYTHON" -m fare_watch.search "$@"
