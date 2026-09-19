# AGENTS.md — 給 AI agent 的操作指南

這個專案查 **Google Flights 當下顯示的直飛票價**，全部在使用者本機執行。
讀完這份你應該能：幫使用者查票、解讀結果、改程式而不破壞既有規則。人類版說明在 [README](README.md)，規則細節在 [docs/reference.md](docs/reference.md)。

## 先決條件

```bash
./scripts/setup.sh        # 建 .venv、裝套件、下載 Chromium。可重複執行，已裝好會很快結束
```

`scripts/` 底下的 script 一律自己挑直譯器（`FARE_WATCH_PYTHON` → `.venv` → `python3`），**不要**自己 `pip install` 到系統 Python。
所有指令從專案根目錄執行。

**Windows（沒有 bash）**：安裝與開網頁用 `scripts\web.bat`；其餘指令把 `./scripts/xxx.sh` 換成對應的模組，並先設 `PYTHONUTF8=1`：
`search.sh` → `.venv\Scripts\python -m fare_watch.search`、`holidays.sh` → `.venv\Scripts\python -m fare_watch.holiday_deals`，參數完全相同。
Windows 這條路尚未在實機驗證；出錯時把完整錯誤訊息回報給使用者，不要自行改用別的資料來源。

## 使用者想做什麼 → 你該跑什麼

| 使用者的話 | 指令 |
|---|---|
| 「最近有哪些連假？」 | `./scripts/holidays.sh`（不查價，立刻回 JSON） |
| 「中秋連假去哪便宜？」 | 先跑上一行拿到 `start`/`end`，再 `./scripts/holidays.sh TPE <start> <end> --json` |
| 「中秋連假去香港多少？」（連假＋已指定地點） | 先跑第一行，從 `options[]` 挑使用者要的走法，再用它的 `depart`/`return` 跑下面的 `search.sh`——只要 2 次查詢，不必跑 32 次的連假比價 |
| 「那個連假也幫我看札幌」 | `./scripts/holidays.sh TPE <start> <end> --destination CTS --json`（只多查 4 次，併進既有報告） |
| 「1/20 去、1/24 回，台北到大阪多少？」 | `./scripts/search.sh TPE KIX 2027-01-20 2027-01-24 --json` |
| 「1/20 單程」 | `./scripts/search.sh TPE KIX 2027-01-20 --json` |
| 「一月哪幾天去大阪最便宜？住 4 晚」 | `./scripts/search.sh TPE KIX --start 2027-01-01 --end 2027-01-31 --nights 4 --json` |
| 「CI156 這班最近漲還是跌？」 | `./scripts/search.sh history TPE KIX --depart 2027-01-20 --flight CI156`（不連網，讀本機歷史） |
| 「我想自己在網頁上看」 | `./scripts/web.sh`（會開瀏覽器並佔住終端機；你自己要用就加 `--no-open` 並放背景） |

機場可以用三碼（`TPE`、`KIX`）或內建中文別名（台北、高雄、大阪、成田、羽田、福岡、沖繩、札幌、首爾、釜山、香港、曼谷…，完整清單在 `fare_watch/search.py` 的 `ALIASES`）。
**東京一定要指定 `NRT` 或 `HND`**。不在別名表的機場直接給三碼。

想一次比較很多目的地（例如「日本哪裡最便宜」）：沒有內建指令，對每個機場各跑一次 `search.sh … --json`，
讀各自的 `dates[0].min_price` 排序。**一個一個跑，不要平行**；任何一次的 `aborted_reason` 非空就整批停下。

## 時間與成本（先告訴使用者，不要讓他乾等）

- 每次「查詢」= 一個方向、一天、一條航線，約 **15 秒**（開真的瀏覽器）。來回 = 2 次；掃 30 天 = 最多 60 次
- 連假查價 = 8 個地點 × 4 次 = 32 次，約 8 分鐘
- 成功的結果快取 **6 小時**：重跑同一條不會再打來源。只有使用者明確要「最新價格」才加 `--refresh`
- 超過 2 分鐘的工作請放背景執行，進度會逐行印在 stderr：`[3/32] 2026-09-25 TPE→OKA ok (live)`

## 怎麼讀結果

**stdout 只有 JSON；進度與報告檔路徑都在 stderr。** 解析時分開接（`> out.json 2> err.txt`），不要 `2>&1`，否則 JSON 後面會多出一行「報告：…」而解析失敗。

**結束碼**：`0` = 全部查完且有可排名的價格；`2` = 部分失敗、被擋或沒有價格（stdout 仍然有已完成的部分，照樣要讀）。

`search.sh … --json` 的重點欄位：

```
aborted_reason            非空 = 被來源擋下，整輪已停
dates[]                   每個出發日一筆，兩種模式都有（指定日期時只有一筆，用 dates[0]）
  .min_price              這組日期最便宜的「去程+回程」合計；null = 排不出來
  .outbound / .inbound    { status, reason, fetched_at, provenance: live|cache, flights[] }
    .flights[]            { airline, flights:["CI156"], depart_time, arrive_time, arrive_date, price, status }
  .combinations[]         { outbound, inbound, price }，已依價格排序
ranked[]                  dates[] 裡有價格的那些，便宜到貴；掃描模式看這個找便宜日期
```

`holidays.sh`（不帶參數）：`breaks[]` = `{ name, start, end, days, options[] }`，`options[]` 是 4 種請假走法
`{ depart, return, total_days, leave_days }`。`errors[]` 非空表示辦公日曆表沒抓到或沿用舊檔。

`holidays.sh <origin> <start> <end> --json`：`deals[]` 已依價格排序，每筆
`{ destination, depart, return, leave_days, total_days, price, outbound, inbound }`；同一個 `destination` 會出現多次
（每種請假走法一筆，就是不帶參數時 `options[]` 的那 4 種，用 `depart`+`return` 對應），第一次出現的就是該地點最便宜的。

報告檔也會落地：`data/search/latest.json`、`data/deals/<origin>-<start>-<end>/latest.json`；
每次**實際連網**的報價永久寫進 `data/search/history.sqlite3`。

## 回報給使用者時一定要講的事

1. 價格是**每位成人、經濟艙、直飛、TWD、兩張單程相加**——不是航空公司的來回票，傳統航空的來回票通常更便宜
2. 不含行李，票規沒核對；這是來源「當下顯示的起價」，附上 `fetched_at`
3. `price: null` / `status: schedule_only` = 有這班但來源沒給價格，**不是 0 元，也不是售完**
4. 某機場沒結果 = 那天來源沒顯示直飛，不代表航線不存在
5. 最便宜的組合常常是紅眼或清晨回程，把起降時間一起列出來讓使用者判斷

## 紅線（改程式或操作時都不能違反）

- **不捏造**：查不到就回報查不到。不估算、不拿舊價格當新價格、不把 null 當 0
- **被擋就停**：遇到 403／429／驗證碼／同意頁，程式會丟 `BlockedError` 並中止整輪。**不要**重試、換 User-Agent、解驗證碼、掛代理、縮短間隔或平行查詢來繞過；把已完成的結果和 `aborted_reason` 交給使用者
- **不猜日曆**：連假只能來自人事行政總處的官方 CSV。抓不到就說抓不到，不用「週一到週五上班」推算
- **真的查到過的價格不被失敗蓋掉**：中止的重新查價不覆寫上一份完整報告
- **個資**：這是公開 repo。不要把使用者的姓名、email、居住地、本機絕對路徑（`/Users/...`）寫進任何會被 commit 的檔案；`data/` 已在 `.gitignore`，不要加進版控

## 改程式

```bash
.venv/bin/python -m pytest        # 168 個測試，全部離線，約 2 秒。改完一定要全過
```

| 檔案 | 職責 |
|---|---|
| `fare_watch/search.py` | 任意航線查價：瀏覽器來源、快取、排組合、CLI |
| `fare_watch/holiday_deals.py` | 連假 × 多目的地的編排、併報告、CLI |
| `fare_watch/dgpa_calendar.py` | 下載／解析辦公日曆表、找連假、排請假走法 |
| `fare_watch/price_history.py` | SQLite 票價歷史與報表 |
| `fare_watch/web_server.py` + `web/` | 只聽 127.0.0.1 的網頁；首頁邏輯在 `web/holiday.js` |
| `fare_watch/cli.py`、`grouped.py`、`itineraries.py`… | 最早的 TPE/KHH→PUS 定點監測，與上面各自獨立 |

慣例：

- 註解用繁體中文，寫「為什麼」不寫「做什麼」；log 與錯誤訊息的字串維持原樣
- 測試不連網：瀏覽器來源用替身（見 `tests/test_holiday_deals.py` 的 `FakeSource`），日曆用 `tests/fixtures/` 的官方 CSV。**production 路徑不吃 fixture**
- 網頁有嚴格 CSP：**不能**用 inline `style=`、`<style>`、inline script、外部字體或 CDN。樣式一律寫 class 進 `web/style.css`
- 網頁的 POST 要帶 `X-Request-Token`，而且只接受日曆上算得出來的連假日期——不要為了方便放寬
- Google Flights 的頁面結構會變。解析壞掉時先跑一次真實查詢看 `reason` 欄位斷在哪，不要先調參數
