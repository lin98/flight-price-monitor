#!/bin/bash
# 不帶參數：列出即將到來的連假（JSON）。帶「出發機場 連假第一天 連假最後一天」：查這個連假各地點的票價。
set -euo pipefail
cd "$(dirname "$0")/.."
# 直譯器順序：FARE_WATCH_PYTHON → 專案的 .venv（scripts/setup.sh 建的）→ PATH 上的 python3
if [ -n "${FARE_WATCH_PYTHON:-}" ]; then PYTHON="$FARE_WATCH_PYTHON"
elif [ -x .venv/bin/python ]; then PYTHON=".venv/bin/python"
else PYTHON="python3"; fi
exec "$PYTHON" -m fare_watch.holiday_deals "$@"
