#!/bin/bash
# 一次裝好：在專案內建 .venv、裝相依套件、下載查價用的 Chromium。可重複執行。
# 用專案自己的 .venv，是為了不碰系統 Python，也讓其他 script 不必猜套件裝在哪個直譯器。
set -euo pipefail
cd "$(dirname "$0")/.."

# 不直接信任 PATH 上的 python3：有些環境的 Python 裝得不完整（例如部分 macOS 上的 Homebrew Python
# 載不了 pyexpat），建 venv 時才會在 ensurepip 噴一大段看不懂的錯。所以逐一試，挑第一個真的能用的。
usable() {
    command -v "$1" >/dev/null 2>&1 && "$1" -c \
        'import sys, ensurepip, pyexpat, sqlite3, ssl, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}
PYTHON=""
for candidate in ${FARE_WATCH_PYTHON:-} python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if usable "$candidate"; then PYTHON="$candidate"; break; fi
done
if [ -z "$PYTHON" ]; then
    echo "找不到可用的 Python 3.9 以上。請安裝 https://www.python.org/downloads/ 的版本，" >&2
    echo "或用 FARE_WATCH_PYTHON=/path/to/python3 ./scripts/setup.sh 指定。" >&2
    exit 1
fi
echo "使用 $("$PYTHON" --version 2>&1)（$(command -v "$PYTHON")）"

# 上一次用壞掉的 Python 建到一半的 .venv 沒有 pip，留著會讓這次也失敗
if [ -d .venv ] && ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    echo "偵測到不完整的 .venv，重新建立"
    "$PYTHON" -m venv --clear .venv
fi
[ -x .venv/bin/python ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python -m playwright install chromium

echo
echo "安裝完成。啟動網頁：./scripts/web.sh"
