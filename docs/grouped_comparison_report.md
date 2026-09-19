# 三人分組比價功能報告（2026-08-29）

> **後續**：本文寫成時唯一能用的來源只解得到去程，且每輪都要重抓整個視窗。
> 之後改用真實瀏覽器抓回程、並改成分批掃描，見
> [`incremental_scan_report.md`](incremental_scan_report.md)。
> 本文中「Google 分享頁只伺服端渲染去程，回程一律 `return_time_unavailable`」的敘述
> 僅適用於 `--source google`；`google_playwright` 解得到回程。

## 需求摘要

- 三位旅客：旅客A（只能 TPE）、旅客B（TPE 或 KHH）、旅客C（只能 KHH）；台中 RMQ 一律排除
- 同一天出發、同一天回程，5 天 4 夜；可搭不同班機；只看直飛
- 去程抵達 PUS 時間差 ≤ 120 分鐘（`--arrival-gap-min` 可調）
- **只列每人單價，不計算也不輸出合計**
- 出發日涵蓋 2027-03-01 ～ 2027-05-31 每一天（含 5/28～5/31，回程落在 6 月初）
- 輸出：日期、每人出發機場、航班、去程起飛/抵達時間、每人票價、抵達差、請假天數、紅眼/深夜標籤
- 排序：正常時段優先 → 抵達差 → 每人票價
- 回程時間解析不到就標 `return_time_unavailable`，不推估

## 實作

| 檔案 | 改動 |
|---|---|
| `fare_watch/grouped.py`（新） | 旅客設定 `TRAVELERS` / `EXCLUDED_ORIGINS`、`usable_offers()`（直飛＋抵達可解析＋同班機留最低價）、`build_options()`（枚舉三人組合並套抵達差門檻）、`option_sort_key()`、`build_grouped_summary()`、`render_grouped_markdown()` |
| `fare_watch/itineraries.py` | `generate()` 加 `depart_through` 參數；預設行為不變（88 個行程），分組模式傳 `WINDOW_END` 得 92 個 |
| `fare_watch/storage.py` | `save_raw()` / `append_observation()` 加 `origin`（raw 多一層機場目錄；觀測去重鍵含機場，舊紀錄格式不變）；新增 `write_grouped()` 寫 `data/grouped_latest.{json,md}` 與 `data/grouped/grouped_<run_id>.*` |
| `fare_watch/cli.py` | `--grouped` 旗標、`make_fetcher(args, origin)`、`make_grouped_fetchers()`、`run_grouped_once()`、`print_grouped_summary()`；`main()` 依旗標分流，`--loop` 亦可用。`--passengers` 的 help 字串原本寫「另列 3 人合計」，改為「不列合計」 |
| `fare_watch/__init__.py` | 模組清單加 `grouped`，版本 0.2.0 → 0.3.0 |
| `tests/test_grouped.py`（新） | 16 個測試，見下 |
| `README.md` | 新增「三人分組比價」一節 |

### 設計決定

- **排序中的「每人票價」怎麼比而不加總**：把三人票價由高到低排成 tuple 逐位比較（先比最貴的那位）。
  JSON 裡沒有任何 total / sum 欄位，測試會遞迴檢查 key 並確認三人票價相加的數字不出現在輸出中。
- **抵達差優先於票價**是規格指定：實測 3/2 的最佳組合是 KE2086（TPE，9,495）＋7C6256（KHH，9,738）差 10 分，
  而不是最便宜的 IT606（7,623）＋BX586（8,642）差 95 分。每個日期的其他可行組合放在 `dates[].alternatives`（最多 3 個）。
- **`normal_hours` 的定義**：組合內沒有 `red_eye`（06:00 前起飛）、`late_departure`（21:00 後起飛）、`early_return`。
  `return_time_unavailable` 只是資訊不足，不會把組合踢出「正常時段」，否則現階段所有組合都會被判非正常。
- **抵達時間只有 HH:MM**：若有跨午夜抵達（例如 23:50 vs 00:20）會算成 1410 分而被排除——寧可漏掉也不猜日期。
- **每個日期只抓 TPE、KHH 各一次**（旅客B 共用這兩份結果），不是三人各抓一次。
- 5/28～5/31 出發的回程落在 6/1～6/4，該區間無國定假日，請假天數只依平日計算。

### 相容性

- 一般模式（不帶 `--grouped`）的行為、輸出格式、`data/reports/`、`raw/<行程>/`、觀測紀錄格式完全不變；既有 26 個測試未改動且全數通過。
- 舊觀測紀錄沒有 `origin` 欄位，去重鍵維持原格式；分組模式寫入的紀錄多 `origin`。

## 測試

`tests/test_grouped.py`：

- 視窗：預設仍 88 筆；`depart_through=WINDOW_END` 得 92 筆，最後一筆 5/31 → 6/4，請假 5 天
- 旅客/機場設定與 RMQ 排除
- 組合：旅客B 可選 TPE 或 KHH、抵達差過濾與原因碼、缺機場報價原因碼、只留直飛、抵達時間解析不到排除、同班機留最低價
- 排序：正常時段 → 抵達差 → 每人票價
- 標籤：紅眼、深夜出發、`return_time_unavailable`、有回程時間才 `normal`
- 摘要：結構、計數、**無 total/sum 欄位、加總數字不出現、「合計」不出現**
- Markdown：欄位與內容
- CLI：`--grouped --dry-run` 產生 `grouped_latest.{json,md}`（31 個 5 月日期、62 次 dry_run、最後一天 5/31→6/4、URL 無 RMQ）；
  兩機場都抓、被封鎖即中止並把剩餘標 unavailable、觀測紀錄含 origin；`--origin` 在分組模式被忽略
- Storage：觀測去重以機場區分，舊格式互不干擾
