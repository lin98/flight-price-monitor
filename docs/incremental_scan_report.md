# 分批掃描與 Playwright 來源（2026-08-30）

接續 [`grouped_comparison_report.md`](grouped_comparison_report.md)。上一份報告完成三人分組比價的
邏輯與輸出；那時唯一能用的來源只看得到去程，且每輪都要重抓整個視窗。這一輪處理的是「**真的抓得到、
而且抓得完**」。

## 問題

1. **回程抓不到**：Google Flights 的來回查詢，第一頁只列去程卡片（卡片上的價格是整趟來回的最低估計
   總價），回程航班要點掉某張去程卡片、進到回程選擇頁才會出現。純 HTTP 的 `GoogleFlightsFetcher`
   連第一頁都拿不到（`dynamic_content`）。航班號也只在展開「航班詳細資料」之後才有。
2. **一輪跑不完**：整個視窗是 92 個出發日 × 2 個機場 = **184 格**，用瀏覽器跑一格約 50 秒，
   一次跑完要三小時以上；中途被擋就整輪白費，而且報告只反映那一輪抓到的東西。
3. **排程是壞的**：plist 指向 `/opt/homebrew/bin/python3`（本機沒裝 playwright），
   而且跑的是單機場 HTTP 模式，不是分組模式。

## 實作

| 檔案 | 改動 |
|---|---|
| `fare_watch/playwright_fetcher.py` | 真實瀏覽器跑完兩階段來回流程；解析層（`parse_leg` / `parse_result_html` / `build_offers`）與瀏覽器層（`_playwright_render`）分開，`render=` 可注入替換 |
| `fare_watch/fetchers.py` | `Offer.from_dict()` / `FetchResult.from_dict()`：把落地的 raw JSON 還原成物件，`to_dict()` 多寫的衍生欄位（`time_tags`、`min_price`）是 property，還原時濾掉 |
| `fare_watch/storage.py` | `latest_raw()` / `latest_raw_by_origin()`：讀回某（機場, 行程）最後一次落地的結果。檔名帶時間戳，同目錄字典序＝時間序，取最大的即可，不必逐檔開來比 `fetched_at` |
| `fare_watch/cli.py` | `--dates`（複查特定日期）、`--max-fetches N`（這輪最多抓 N 格）、`--merge-stored`（沿用先前落地的結果）；離開碼語意改寫 |
| `fare_watch/grouped.py` | `_fetch_cell()` 加 `provenance`；新增 `coverage` 區塊與 Markdown 的「涵蓋範圍」表 |
| `scripts/run_grouped.sh`（新） | 排程入口：選對直譯器、檢查 playwright、跑一輪分批掃描 |
| `launchd/com.example.fare-watch.plist` | 改成呼叫上面的 script，帶 `FARE_WATCH_PYTHON` / `FARE_WATCH_MAX_FETCHES` |
| `tests/test_incremental.py`（新） | 16 個測試，見下 |
| `README.md` / `.env.example` | 來源一節重寫（原本寫「唯一的價格來源是 Amadeus」已不成立）、分批掃描、排程 |

### 設計決定

- **選擇器一律留退路**：Google 的 class 是混淆字串（`li.pIav2d`、`gvkrdb`…）隨時會換。每一步都是
  「候選選擇器逐一嘗試 → 退到可見文字比對」，命中哪一個記進 `FetchResult.reason`，壞掉時看 reason
  就知道斷在哪一層。
- **抓不到就留空，不推估**：展不開明細就只讀摘要（少航班號，時刻與直達照樣解得出來）；解析不到回程
  時刻標 `return_time_unavailable`；解析不到時長給 `0` 且報告不顯示，不印 `0h00m`。
- **哪一格先抓**：`--max-fetches` 依「沒抓過的優先 → 資料最舊的優先」排序，選完再照日期順序去抓
  （log 比較好讀）。這樣冷啟動會先鋪滿整個視窗，之後才輪流刷新。
- **被擋不覆蓋舊資料**：被擋要中止整輪，但沿用的舊價格是真的抓到過的，不該被 `unavailable` 蓋掉；
  只有沒有舊資料的格子才標 `aborted_after_block`。
- **每一格標 `provenance`**：`this_run` / `stored` / `none`，配上各自的 `fetched_at`。
  分批掃描下報告必然混著新舊資料，不標出來就沒人分得清哪些數字是這輪的。
- **離開碼**：`0` = 沒被擋且這輪抓的至少有一格 `ok`；`2` = 被擋，或這輪抓的每一格都失敗。
  「還湊不出三人組合」**不**算失敗——才掃半個視窗當然可能沒有組合，讓它回非 0 只會讓 launchd
  每天亮紅燈，真正被擋的那天反而沒人注意。
- **`--dates` 只收 `YYYY-MM-DD`**：Python 3.11 的 `date.fromisoformat` 連 `20270401` 都吃，
  但那在不同版本行為不一樣，統一要求一種寫法。超出視窗直接報錯，不靜靜過濾成空跑。

## 排程

`scripts/run_grouped.sh` 每輪抓 `FARE_WATCH_MAX_FETCHES`（預設 24）格、約 22 分鐘；
launchd 每天 08:00 與 20:00 各一輪 = 一天 48 格，184 格的完整視窗約 **4 天**輪完一次。
報告每輪都是完整視窗（沒抓到的格子沿用先前落地的結果並標明時間）。

plist 走 shell script 而不是直接 `python -m fare_watch`：launchd 不讀 shell profile，
`PATH` 裡的 `python3` 可能是沒裝 playwright 的那一版；script 先檢查直譯器與套件，缺就以
exit 78（`EX_CONFIG`）明確失敗，不要安靜地跑成別的模式。

## 測試

`tests/test_incremental.py`：

- 還原：`Offer` / `FetchResult` round trip，衍生欄位濾掉、`html` 不無中生有
- 讀回：取最新那份（不是最便宜那份）、沒有就回 `None` 不猜
- `--dates`：解析與排序、拒絕格式錯誤與視窗外日期、真的過濾行程
- `--max-fetches`：限制格數、其餘標 `missing`/`not fetched yet`、沒抓過的優先其次最舊的
- `--merge-stored`：沿用的格子標 `stored` 並保留原 `fetched_at`、能湊出組合、
  被擋不覆蓋舊價格
- `coverage`：各機場狀態與 provenance 統計、資料最舊/最新時間、Markdown 的「涵蓋範圍」表
- 離開碼：被擋 → 2、這輪全失敗 → 2、抓到了但還湊不出組合 → 0

`tests/test_playwright_fetcher.py` 用 `tests/fixtures/` 的頁面快照測解析層，瀏覽器層以替身注入。
**fixture 只餵給解析函式，production 路徑不吃 fixture。**

## 誠實記錄：來源限制

- 被擋（HTTP 403/429、`/sorry/`、captcha、consent 頁）一律丟 `BlockedError` 中止整輪，理由記進
  `run.aborted_reason`。不換 UA 重打、不解 captcha、不硬跳同意頁、不輪替代理。
- Amadeus test 環境對 2027 年的日期幾乎沒有資料，實務上拿不到報價；這是來源本身的限制，
  不會用估算值補。
- Google Flights 對 2027 年這種遠期日期，部分航線本來就還沒開賣；那會回 `unavailable`
  而不是 `ok`，報告照實列原因碼。
