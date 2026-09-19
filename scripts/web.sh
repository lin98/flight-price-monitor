#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
# 直譯器順序：FARE_WATCH_PYTHON → 專案的 .venv（scripts/setup.sh 建的）→ PATH 上的 python3
if [ -n "${FARE_WATCH_PYTHON:-}" ]; then PYTHON="$FARE_WATCH_PYTHON"
elif [ -x .venv/bin/python ]; then PYTHON=".venv/bin/python"
else PYTHON="python3"; fi
exec "$PYTHON" -m fare_watch.web_server "$@"
