# 功能細節與參考

[README](../README.md) 只講怎麼跑起來；這裡是每個功能的規則、參數與設計取捨。給 agent 的操作指南在 [AGENTS.md](../AGENTS.md)。

以下 `python` 指令請先 `source .venv/bin/activate`；`scripts/` 底下的 script 會自己用 `.venv`，不需要先啟用。

**平台**：在 macOS 上開發與測試。程式本身不依賴 POSIX 專屬功能（不用 symlink、檔案讀寫一律明寫 UTF-8、
Windows 會另外安裝 `tzdata` 提供時區資料），Windows 入口是 `scripts\web.bat`，但**尚未在 Windows 實機驗證**。
排程一節的 launchd 只適用 macOS。

## 定點監測（最早的功能）

固定監測 TPE/KHH→PUS、2027/3–5 月的 5 天 4 夜行程。以下 `python` 指令請先啟用 `scripts/setup.sh` 建好的環境：

```bash
source .venv/bin/activate
```

`scripts/` 底下的 script 會自己用 `.venv`，不需要先啟用。要指定別的直譯器設 `FARE_WATCH_PYTHON`。

```bash
# 只列行程與請假天數，不查價
python flight_monitor.py --dry-run

# 只看 3 月出發的行程，JSON 輸出
python flight_monitor.py --dry-run --month 3 --json

# 查真實價格（報價均為每人單價；3 人同行只作為搜尋條件，不計算合計）
python -m fare_watch --grouped --source google_playwright --dates 2027-03-01
```

| 參數 | 說明 |
|---|---|
| `--source` | `fli` 先問可選的 fli 套件、失敗自動退回瀏覽器（預設）、`google_playwright` 只用瀏覽器、`google` 純 HTTP、`amadeus` API |
| `--origin` | `TPE` 桃園或 `KHH` 高雄；台中 RMQ 排除，目的地固定 `PUS` |
| `--passengers` | 同行人數，預設 3；報告只顯示每人價格 |
| `--arrival-gap-min` | 同日不同班機的抵達時間最大差距，預設 120 分鐘 |
| `--month {3,4,5}` | 只列該月**出發**的行程 |
| `--dates` | 只看指定出發日（逗號分隔 `YYYY-MM-DD`），用來複查特定日期 |
| `--max-fetches` | 這輪最多抓幾格（一格＝一個出發日 × 一個機場），見「分批掃描」 |
| `--merge-stored` | 把 `data/raw/` 裡先前落地的結果併進報告 |
| `--dry-run` | 不呼叫任何價格來源，`price.status` 一律為 `unavailable` |
| `--json` | 以 JSON 輸出（否則印表格） |
| `--grouped` | 分組比價模式，見下節；忽略 `--origin`。**預設只找旅客A（TPE）** |
| `--all-travelers` | 改查完整三人（旅客A TPE、旅客B TPE/KHH、旅客C KHH）並套用抵達差門檻 |

## 分組比價（`--grouped`）

**預設只找旅客A（TPE → PUS）**——最常見的用法是一個人查票。三人分組的邏輯完整保留，
加 `--all-travelers` 打開：

```bash
python -m fare_watch --grouped --dates 2027-03-11                    # 預設：只有旅客A TPE
python -m fare_watch --grouped --all-travelers --dates 2027-03-11    # 三人：TPE + KHH
```

只查旅客A 時：只打 TPE 一次（查詢量減半）、報告標題是「旅客A機票比價」、表格只有一欄旅客、
抵達時間差恆為 0（沒有第二個人要會合，門檻不會擋掉任何班機）。

### `--all-travelers`：三人分組

三個人從不同地方出發、各自買票、同一天出發同一天回來（5 天 4 夜）、在釜山機場會合：

| 旅客 | 可用出發機場 |
|---|---|
| 旅客A | TPE |
| 旅客B | TPE 或 KHH |
| 旅客C | KHH |

台中 RMQ 一律排除。規則：

- 只看直飛；三人可搭不同班機，但**去程抵達 PUS 的時間差 ≤ 120 分鐘**（`--arrival-gap-min` 可調）
- 出發日涵蓋 **2027-03-01 ～ 2027-05-31 每一天**（5/28～5/31 出發的回程落在 6 月初，一般模式不含這幾天）
- **只列每人單價，不計算、不輸出三人合計**；排序比較票價時也只逐位比較單人票價
- 排序：正常時段（無 `red_eye` / `late_departure` / `early_return`）優先 → 抵達差小者優先 → 每人票價（先比組合裡最貴的那位）→ 日期
- 回程時間：`google_playwright` 來源會真的點進回程選擇頁，解得到就照實列；解不到（或用只看得到去程的
  `google` 來源）就標 `return_time_unavailable`，不用去程時間推估。標籤定義同 `time_tags`

```bash
# 分批掃描：這輪只抓 24 格，其餘沿用先前落地的結果（排程就是跑這個）
./scripts/run_grouped.sh

# 複查特定日期
python -m fare_watch --grouped --source google_playwright --dates 2027-03-15,2027-04-15 --merge-stored

# 只看 5 月、不連網先看行程與查詢連結
python -m fare_watch --grouped --dry-run --month 5 --json
```

### 分批掃描（`--max-fetches` / `--merge-stored`）

整個視窗是 92 個出發日 × 2 個機場 = **184 格**，用瀏覽器跑一格約 50 秒，一次跑完要三小時以上，
中途被擋就整輪白費。所以：

- `--max-fetches N`：這輪最多抓 N 格，**沒抓過的優先、其次資料最舊的**，抓完就寫報告
- `--merge-stored`：這輪沒抓到的格子，從 `data/raw/<機場>/<行程>/` 讀回最後一次落地的結果併進報告

每一格都標 `provenance`：`this_run`（這輪剛抓）/ `stored`（沿用先前）/ `none`（還沒抓過），
搭配各自的 `fetched_at`，所以看得出報告裡哪些數字是新的。報告的 `coverage` 區塊與 Markdown
的「涵蓋範圍」表列出：視窗共幾天、本報告涵蓋幾天、每個機場的抓取狀態、資料最舊/最新時間。

被擋（403 / 429 / captcha / consent）時整輪立刻中止、不重試也不繞過；**已經沿用的舊資料不會被
`unavailable` 蓋掉**（那是真的抓到過的價格），只有沒有舊資料的格子標 `aborted_after_block`。

輸出：

- `data/grouped_latest.json`：`ranked`（每個日期的最佳組合，已排序）、`dates`（每個日期的 `best` / `alternatives` /
  兩個機場的抓取狀態 / 湊不出組合的原因 `no_option_reason`）、`coverage`（涵蓋範圍與各機場資料新舊）。
  每位旅客有 `origin`、`flight`、`depart_time`、`arrive_time`、`fare`、`labels`、`return_time_status`
- `data/grouped_latest.md`：同內容的表格版；另在 `data/grouped/grouped_<run_id>.*` 留每輪歷史
- **日期顯示格式**：Markdown 報告與 CLI 摘要的出發日／回程日一律是 `M/D(星期)`，例如 `3/11(四)`；
  JSON 的 `depart` / `return` / `key` 與查詢 URL 維持 ISO `YYYY-MM-DD`（排序、去重、URL 都吃它）
- raw 與觀測紀錄同一般模式，但多一層機場：`data/raw/<TPE|KHH>/<行程>/...`，
  `fare_watch_observations.jsonl` 每筆多 `origin` 欄位（去重時把機場算進去）

湊不出組合的原因碼：`no_offers_for:<旅客>`（該旅客所有可用機場都沒報價）、`arrival_gap_over_<N>`（有報價但抵達差都超過門檻）。

## 排程（launchd）

```bash
# 先把 plist 裡的 /path/to/flight-price-monitor 換成你的實際路徑
cp launchd/com.example.fare-watch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.fare-watch.plist
launchctl start com.example.fare-watch        # 立刻跑一輪確認
tail -f data/logs/fare_watch.log
```

- 每天 08:00 與 20:00 各跑一輪 `scripts/run_grouped.sh`，每輪 `FARE_WATCH_MAX_FETCHES`（預設 24）格、
  約 22 分鐘；一天 48 格，184 格的完整視窗約 **4 天**輪完一次
- plist 走 shell script 而不是直接 `python -m fare_watch`：launchd 不讀 shell profile，
  `PATH` 裡的 `python3` 可能是沒裝 playwright 的那一版；script 會優先用專案的 `.venv`，缺套件就以 exit 78 明確失敗
- 換機器時要改 plist 裡的專案路徑

## 價格來源與「不捏造」原則

四個來源，預設是第一個：

| `--source` | 說明 | 回程時間 |
|---|---|---|
| `fli` | **預設**。先問 fli（見下節），拿不到就自動退回 `google_playwright` | 有 |
| `google_playwright` | 真實瀏覽器跑完 Google Flights 的兩階段來回流程：先讀去程卡片，再點進去拿回程選擇頁。也是 `fli` 的 fallback | 有 |
| `google` | 純 HTTP 抓可分享查詢頁。Google 只伺服端渲染去程，多半連第一頁都拿不到（`dynamic_content`） | 無 |
| `amadeus` | [Amadeus Flight Offers Search](https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-search) test 環境，需自備免費憑證 | 有，但 test 環境對 2027 年的日期幾乎沒資料 |

為什麼非得用瀏覽器：Google Flights 的來回查詢，第一頁只列**去程**卡片（卡片上的價格是整趟來回的最低估計
總價），回程航班要點掉某張去程卡片、進到回程選擇頁才會出現。航班號也只在展開「航班詳細資料」後才有。

以下任一情況都回 `{"status": "unavailable" | "error", "reason": "..."}`，**不會**回傳估算值、快取值或預設值：

- 網路錯誤、HTTP 非 2xx、回應/頁面結構解析不出來
- 來源對該日期沒有任何報價
- 解析不到回程時刻 → 標 `return_time_unavailable`，不用去程時間或常識推估
- 解析不到時長 → `duration_min: 0`，報告不顯示而不是印 0h00m

### fli 是可選捷徑，不是必要條件

[fli](https://github.com/punitarani/fli)（PyPI 套件名 **`flights`**、import 名 **`fli`**、MIT）直接打
Google Flights 的內部端點，不開瀏覽器。實測同一組查詢：**fli 約 1～5 秒，瀏覽器約 50 秒**。

**它刻意不列進 `requirements.txt`**，沒裝也不影響任何功能：

```bash
pip install flights        # 想用才裝；不裝就一直走 google_playwright
```

任何一項不對就安靜退回 `google_playwright`，理由記進 `FetchResult.reason`（`fli_fallback[...]`）：
沒安裝、查詢失敗、回空、缺去程或回程 legs、`price` 是 `None`、非直飛、方向不對、時刻解不出來。

實測驗出來的三個坑，程式都處理了：

- **`adults=3` 回的是三人總價**（18,669 = 6,223×3）。本專案只列每人單價，所以**一律用 `adults=1` 查**，不用除法
- **來回 pair 的價格要取回程那一筆**：去程 `KE2086` 9,078 配回程 `CI187` 會顯示 12,522，取去程會低估
- **`price=None` 真的會出現**（實測 `BR163`）：直接丟掉該組合，不當 0

另外 `Airline` enum 名字不能以數字開頭，濟州航空是 `_7C`，會正規化回 `7C`。

報告的每一格都記 `source`（`fli` 或 `playwright`），「涵蓋範圍」表的「實際來源」欄位看得出
哪些日期走了慢路。

**fli 失敗不算被擋**：它只是可選捷徑，走不通就換路；被不被擋由 `google_playwright` 判斷，
它丟的 `BlockedError` 照舊往上冒、中止整輪。

**被擋就停**：HTTP 403/429、`/sorry/`、captcha、consent 頁一律丟 `BlockedError` 中止整輪，
理由記進 `run.aborted_reason`。不換 UA 重打、不解 captcha、不硬跳同意頁、不代理輪替——
被擋是被擋，記錄下來比繞過去重要。測試用的 fixture 只餵給解析函式，**production 路徑不吃 fixture**。

已知限制：Google Flights 的頁面 class 是混淆字串（`li.pIav2d`、`gvkrdb`…）隨時會換。每一步都是
「候選選擇器逐一嘗試 → 退到可見文字比對」，實際命中哪一個記進 `FetchResult.reason`，壞掉時看 reason
就知道斷在哪一層。

## 請假天數計算

`leave_days` = 行程期間內的平日（週一～週五）扣掉國定假日；若有補班日則補班日算需請假。
分組模式 6/1～6/4 的回程日只依平日計算（該區間無國定假日）。

## 2027 台灣假日資料

僅收錄監測區間（3/1～5/31）內的日期，定義在 `flight_monitor.py` 的 `TAIWAN_HOLIDAYS_2027`：

| 日期 | 說明 |
|---|---|
| 2027-03-01（一） | 和平紀念日 2/28 逢週日，補假 |
| 2027-04-04（日） | 兒童節 |
| 2027-04-05（一） | 民族掃墓節（清明） |
| 2027-04-06（二） | 兒童節逢週日，於清明節次日補假 |
| 2027-04-30（五） | 勞動節 5/1 逢週六，提前補假 |
| 2027-05-01（六） | 勞動節 |

補班日：官方公告 2027 全年無補班日（`MAKEUP_WORKDAYS_2027` 為空）。

**來源**：行政院人事行政總處新聞稿〈行政院核定 116 年（西元 2027 年）政府行政機關辦公日曆表〉
<https://www.dgpa.gov.tw/information?pid=12983&uid=82>（2026-08-29 查閱）。

**不確定性**：
- 該日曆表適用於政府行政機關；民間企業依《紀念日及節日實施條例》放假，目前日期一致，
  但雇主可依勞動契約調整，實際請假天數請以自己公司行事曆為準。
- 行政院可能因臨時決議（颱風假、追加彈性放假等）調整，本表不會自動更新，使用前建議再核對一次官方公告。

## 測試

```bash
.venv/bin/python -m pytest
```

168 個測試，全部離線（瀏覽器層在測試裡被替身取代，解析層吃 `tests/fixtures/` 的頁面快照）。
fixture 只用來測解析，不會出現在 production 路徑。

`tests/fixtures/dgpa_calendar_2026.csv`、`dgpa_calendar_2027.csv` 是行政院人事行政總處發布的
「[中華民國政府行政機關辦公日曆表](https://data.gov.tw/dataset/14718)」原檔，依
[政府資料開放授權條款－第 1 版](https://data.gov.tw/license)利用；`dgpa_dataset_index.json` 是同一資料集索引的精簡版（只留檔案描述、格式與位址）。

真的連網的煙霧測試（會開瀏覽器、約 2 分鐘）：

```bash
python -m fare_watch --grouped --source google_playwright --dates 2027-03-01 --data-dir data/smoke
```

## 通用航線查詢（新增）

此入口不受舊工具的 2027/3～5 月、TPE/KHH→PUS 限制。需要 requirements.txt 的
Playwright 與 Chromium；`scripts/search.sh` 使用與既有排程相同的 Python，可用
`FARE_WATCH_PYTHON` 覆寫。也可直接 `python -m fare_watch.search ...`，
或使用既有入口 `python flight_monitor.py search 台北 釜山 2027-03-11 2027-03-15`。

```bash
# 指定去回日期：列出兩天所有來源可見直飛班次、航空公司、航班號、當地起降時間、單程價格
./scripts/search.sh 台北 釜山 2027-03-11 2027-03-15

# 只給一個日期：查單程
./scripts/search.sh TPE PUS 2027-03-11

# 只給航線：比較明天起 30 個出發日，預設 4 晚（5 天 4 夜）
./scripts/search.sh 台北 釜山

# 指定掃描區間與停留天數：月底出發可在下個月回程
./scripts/search.sh TPE PUS --start 2027-03-01 --end 2027-03-31 --nights 4

# 看較短區間、排名前 20；可換任意三碼機場
./scripts/search.sh KHH KIX --days 7 --nights 3 --top 20

# 強制重查／輸出 JSON／只預覽日期連結
./scripts/search.sh 台北 釜山 2027-03-11 2027-03-15 --refresh
./scripts/search.sh TPE PUS --days 7 --json
./scripts/search.sh TPE PUS --days 7 --dry-run
```

- **每位成人、經濟艙、直飛、TWD**。台北預設桃園 TPE，首爾預設仁川 ICN；
  松山、金浦需另指定。東京請明確給 NRT/成田或 HND/羽田。
- **兩張單程相加**：可搭配不同航空公司，指定日期的全部組合保存在 JSON，Markdown
  預設顯示前 10 組。不是航空公司的來回套票，不保證比來回票便宜。行李／票規需另外確認。
- 無價格仍保留航班，標「僅班次資訊」，不以零元排名；跨日到達會列出完整抵達日期。
- 不再只讀前 4 張卡片：等待載入穩定、展開更多航班，再遍歷所有可見結果。
  指定日期時也展開各航班明細；低價掃描省略航班號明細以加快速度。
- **首次掃描不是瞬間完成**：30 天需要至多 60 個單程查詢，瀏覽器全程重用；
  預設快取 6 小時，快取保留原始查詢時間。可用 `--refresh` 或 `--cache-hours 0` 忽略快取。
  選到喜歡的日期後，再用指定日期模式取得航班號與完整組合。
- 輸出到 `data/search/latest.md`、`latest.json`，另保留每輪帶時間的報告；`--output` 可自訂。
- 報告列出日期涵蓋率、每日成功／失敗／未查詢、每筆資料時間。Google 未顯示的班次
  不會憑空補上；「已查到的最低價」不代表全市場最低價，也不保證每家航空公司的班表一致。
- 遇到 403/429/驗證碼立即停止，輸出已完成結果，不繞過封鎖。結束碼 0 代表查詢完整且
  有可排名價格；2 代表失敗、部分結果或無有效價格。失敗結果不快取。

## 同一航班的每日票價歷史

通用查價現在會自動將每次**實際連網**的結果寫入 `data/search/history.sqlite3`，
與可過期的快取分開；改變 `--output` 不會改變歷史資料庫位置，需隔離時指定 `--history-db`。
每次查價後也會產生 `history-TPE-PUS.html/.md/.csv/.json` 等路線報表。

```bash
# 真正重新報價並永久記錄（同一天同價也保留；讀快取不新增觀測）
./scripts/search.sh 台北 釜山 2027-03-11 2027-03-15 --refresh

# 查整條去程航線的歷史，不連網
./scripts/search.sh history 台北 釜山

# 只看 2027/3/11 的虎航 IT606，和它每次報價的漲跌
./scripts/search.sh history TPE PUS --depart 2027-03-11 --flight IT606

# 回程是另一條航線，獨立追蹤
./scripts/search.sh history PUS TPE --depart 2027-03-15 --flight IT607

# 按報價日篩選（不是搭乘日期）
./scripts/search.sh history TPE PUS --since 2026-09-09

# 掃描期間也展開航班號，才能精確追蹤每一班；比只找最低價慢
./scripts/search.sh TPE PUS --start 2027-03-01 --days 7 --details --refresh

# 匯入本功能上線前已保存的通用查價報告，可重複執行，不會重複計數
./scripts/search.sh history TPE PUS --import-reports data/search-validation
```

- 以**來源、航線、搭乘日期、航班號、幣別與查詢條件**辨識同一班。起降時間調整仍留在
  同一航班歷史。未取得航班號者另用航空公司／時刻辨識，明確標示，不能當作已確認的同班。
- 漲跌比較「前一次有價格的觀測」，不是固定昨天。每天可查多次；日表顯示台灣日期的
  最後價、最低、最高與次數；逐筆時間和報價全保留於 Markdown／CSV／JSON。
- 成功但沒有再看到某班記為 `not_returned`，查詢失敗或不完整為 `query_failed_or_partial`；
  只取得無航班號結果而無法確認是否同班為 `identity_unconfirmed`。這些都不代表售罄。
- 無價格為空值，既不算零元，也不以前一天價格偽造今天報價。不同日期的低價班機
  不會混為同一條航班走勢。航班資料只有一天時，圖上只會有一個點。
- 這是該航班**可見單程起價**的變動；未固定賣方、行李及可退改票規，不能認定為完全相同
  票種本身的漲跌。原始舊版的來回最低價紀錄不匯入單程歷史，避免混價。
- HTML 可直接用瀏覽器開啟，不依賴外部網站或 CDN。SQLite 是永久原始紀錄，請一起備份。
- 每日自動查詢需另外啟用排程；只有執行過查價才有該次真實歷史，不會回補未查詢的過去日期。

## 連假便宜機票

網頁首頁的預設分頁。也可以不開網頁、直接跑：

```bash
./scripts/holidays.sh                                  # 列出即將到來的連假與請假走法（JSON，不查價）
./scripts/holidays.sh TPE 2026-09-25 2026-09-28        # 查這個連假：出發機場、連假第一天、最後一天
./scripts/holidays.sh TPE 2026-09-25 2026-09-28 --destination CTS   # 只多查札幌，併進既有報告
```

**連假從哪來**：[行政院人事行政總處「政府行政機關辦公日曆表」](https://data.gov.tw/dataset/14718)。
程式先向政府資料開放平臺的資料集索引問當年度檔案位址（位址裡的 GUID 每年換，不能寫死），
再從 `www.dgpa.gov.tw` 下載官方 CSV（`西元日期,星期,是否放假,備註`，0＝上班、2＝放假），
存到 `data/holidays/<年>.json`，一週內不重抓。今年與明年各抓一份，元旦、春節才接得起來。

- 連續放假 ≥ 3 天（含週末）算一個連假；已經開始的連假不列
- 下載失敗就沿用舊檔並在頁面標示；連舊檔都沒有就顯示「尚無資料」，
  **不會**用「週一到週五上班」去推——那樣會漏掉補假，請假天數就是錯的
- 檔案格式跟預期不同（欄位、0/2 以外的值、天數不足）一律視為失敗，不硬解
- 只接受 `www.dgpa.gov.tw` 的檔案位址；索引裡出現別的主機就拒絕
- 這份日曆適用政府機關，民間企業多數比照，實際請假天數以自己公司行事曆為準

**怎麼排假**：連假是最長的連續放假區間，所以它的前一天與後一天必然是上班日。
每個連假給 4 種走法——照放、前一天請假、後一天請假、前後各請一天——請假天數用官方日曆逐日算。
去程只有 2 個候選日、回程也只有 2 個，所以每個地點 **4 次單程查詢**就能排出全部 4 種組合。

**查哪些地點**：預設桃園 TPE 出發，沖繩、大阪、東京成田、福岡、首爾、釜山、香港、曼谷（共 32 次查詢，約數分鐘），
清單寫在 `holiday_deals.DESTINATIONS`。首頁搜尋列可以改出發地；「想去哪裡」填了就只多查那一個地點的 4 次查詢、
併進既有排行，不會重跑其他地點（命令列是 `--destination CTS`）。排行只列每個地點最便宜的走法，其餘收在下方明細；
按「看全部航班」會帶著日期跳到指定日期查價。

- 第一次打開首頁、最近的連假從沒查過時會自動開始查；之後一律顯示上次結果與查詢時間，
  要新價格請按「重新查價」。其他連假也是按了才查，不會一開頁面就去打來源
- 查完一個地點就先落地、先顯示；中途被擋就停，已查完的地點保留
- 價格規則與[通用航線查詢](#通用航線查詢新增)相同：每位成人、經濟艙、直飛、兩張單程相加，
  沒報價的地點不以 0 元上榜；每次實際查價同樣寫進票價歷史
- 網頁只接受日曆上算得出來的連假日期，不能用任意日期驅動查價

### 本機網頁操作

執行 `./scripts/web.sh`，再開啟 http://127.0.0.1:8765 。保持終端機執行，Ctrl+C 停止。

網頁首頁是連假便宜機票，另提供指定日期查價、固定停留晚數的便宜日期排行，以及同一航班的每日／逐次報價走勢。每次實際查詢自動存入既有 SQLite 歷史資料庫；每日排程尚未啟用。價格為一位成人經濟艙直飛，來回組合是兩張單程相加。服務僅限這台電腦存取。

搜尋完成後，可在「選擇航班與試算價格」分別選擇去程、回程航班；便宜日期掃描也可切換行程日期選班。僅有班次資訊而無報價的航班無法參與試算。單程搜尋只計算去程。

試算另提供每段「另加稅費／其他費用」欄位（每人台幣），僅填訂票網站確認尚未包含的費用；目前來源未提供獨立稅額及含稅狀態，不會自動加稅。留白標為未確認，明確填 0 表示無額外費用。切換航班會清除該段費用，切換日期會重設選擇與費用；手動試算不寫入原始票價歷史。
