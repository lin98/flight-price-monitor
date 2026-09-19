#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
WEB_PYTHON="${FARE_WATCH_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11}"
if [ ! -x "$WEB_PYTHON" ]; then WEB_PYTHON="python3"; fi
exec "$WEB_PYTHON" -m fare_watch.web_server "$@"
