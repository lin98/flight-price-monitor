"""TPE → PUS 2027 年 3–5 月 5 天 4 夜機票價格監測。

唯一 CLI 入口：`python -m fare_watch`（專案根目錄的 `flight_monitor.py` 只是同義包裝）。

- `holidays_tw`   2027 台灣政府行政機關放假資料（唯一真相來源）
- `itineraries`   產生行程並計算請假天數
- `fetchers`      票價來源：Google Flights 可分享查詢頁（預設）、Amadeus、dry-run
- `amadeus`       Amadeus Flight Offers Search client（憑證 / AMADEUS_HOST / 節流 / 重試）
- `storage`       原始結果、去重觀測紀錄、報告寫入 data/
- `report`        整理報告（最低票價候選、請假最少候選）
- `grouped`       三人分組比價（旅客A TPE / 旅客B TPE 或 KHH / 旅客C KHH，抵達差門檻，只列每人單價）
"""

__version__ = "0.3.0"
