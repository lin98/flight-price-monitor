#!/bin/bash
# 排程用的一輪分批掃描：每次只抓一小批，報告靠 --merge-stored 把先前落地的結果併回來。
#
# 為什麼要分批：整個視窗是 92 個出發日 × 2 個機場 = 184 格，用瀏覽器跑一格約 50 秒，
# 一次跑完要三小時以上，中途被 Google 擋掉就整輪白費。每輪只抓 FARE_WATCH_MAX_FETCHES 格
# （預設 24，約 22 分鐘），優先抓沒抓過的、其次資料最舊的，報告每輪都是完整視窗。
#
# 為什麼寫死 python 路徑：launchd 不讀 shell profile，PATH 裡的 python3 可能是沒裝
# 任何來源的那一版（本機 /opt/homebrew/bin/python3 就是）。用 FARE_WATCH_PYTHON 可覆寫。
#
# 來源檢查刻意寬鬆：預設是 fli 優先、playwright 只是 fallback，所以「有 fli 沒 playwright」
# 是完全可用的組合，不該在啟動前就擋掉。只有兩個都沒有才是真的沒救（exit 78）。
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${FARE_WATCH_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11}"
MAX_FETCHES="${FARE_WATCH_MAX_FETCHES:-24}"

if [ ! -x "$PYTHON" ]; then
    echo "fare-watch: python not found at $PYTHON (set FARE_WATCH_PYTHON)" >&2
    exit 78  # EX_CONFIG
fi
"$PYTHON" -c "import fli" 2>/dev/null && HAS_FLI=1 || HAS_FLI=0
"$PYTHON" -c "import playwright" 2>/dev/null && HAS_PW=1 || HAS_PW=0

if [ "$HAS_FLI" = 0 ] && [ "$HAS_PW" = 0 ]; then
    echo "fare-watch: $PYTHON has neither fli nor playwright -- no usable source." >&2
    echo "  playwright: $PYTHON -m pip install -r requirements.txt (然後 $PYTHON -m playwright install chromium)" >&2
    echo "  fli (可選):  $PYTHON -m pip install flights" >&2
    exit 78  # EX_CONFIG
fi
if [ "$HAS_PW" = 0 ]; then
    echo "fare-watch: $PYTHON has no playwright; running fli-only (fli 失敗的日期會標 playwright_unavailable)" >&2
fi

exec "$PYTHON" -m fare_watch \
    --grouped \
    --source fli \
    --merge-stored \
    --max-fetches "$MAX_FETCHES" \
    --once \
    "$@"
