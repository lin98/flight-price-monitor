# flight-price-monitor

在自己電腦上跑的直飛機票查價工具。打開就告訴你：**下一個連假，飛哪裡最便宜、要不要請假。**

連假日期來自行政院人事行政總處的辦公日曆表，票價是 Google Flights 當下顯示的價格。
不需要帳號或 API 金鑰，資料全部留在你的電腦。

![連假便宜機票首頁](docs/screenshots/holiday-deals.png)

## 開始使用

需要 macOS 或 Linux、Python 3.9 以上。

```bash
git clone https://github.com/lin98/flight-price-monitor.git
cd flight-price-monitor
./scripts/web.sh
```

就這樣。第一次執行會自動安裝（約 1 分鐘），之後瀏覽器會自己打開。要停止按 Ctrl+C，下次再跑同一行。

## 怎麼用

- **首頁**列出最近的連假，自動比較 8 個熱門地點（第一次約數分鐘，查完一個先顯示一個）
- **出發地**預設台北，可以改；**想去哪裡**填了（例如「札幌」）就多比較那一個地點
- 點「**怎麼請假**」的選項，看不請假、請 1 天、請 2 天各能去哪、差多少錢
- 卡片上的「**看全部航班**」列出那兩天所有直飛班次；其他分頁可以查任意日期、找便宜日期、看同一班機的票價走勢

價格是每位成人、經濟艙、直飛、兩張單程相加，不含行李。查不到就顯示查不到，不會估算；被來源擋下就停，不會繞過。

<details><summary>裝不起來？</summary>

- 安裝會自己挑一個能用的 Python（部分 macOS 的 Homebrew Python 有問題，會自動跳過）。都不行就裝 [python.org](https://www.python.org/downloads/) 的版本再跑一次
- 8765 埠被占用：`./scripts/web.sh --port 8800`
- 顯示被擋（403／429／驗證碼）：來源的限制，隔一段時間再試

</details>

## 更多

- [功能細節與參考](docs/reference.md)：命令列查價、票價歷史、排程、資料來源與各種規則
- [AGENTS.md](AGENTS.md)：給 AI agent 的操作指南——讓 agent 讀這份，它就知道怎麼幫你查票

## 授權

[MIT](LICENSE)
