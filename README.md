# 高併發台股量化交易監控系統 (Taiwan Stock Quantitative Trading System)

基於 Python 高併發多執行緒架構打造的台股即時掃描、籌碼與量價雙軌策略、學生專屬交易計畫、開盤前國際晨報與 14:30 三大法人盤後結算系統。

---

## 系統核心特色

1. **🌅 開盤前國際總經與夜盤晨報**：
   - 包含**美股四大指數**（道瓊、標普 500、那斯達克、費城半導體）收盤與漲跌幅。
   - 關鍵權值 ADR（台積電 TSM、輝達 NVDA）即時行情。
   - 台灣期交所 (TAIFEX) 昨晚**台指期夜盤**（TX 近月合約最新收盤價、漲跌點數、高低震盪區間與成交口數）。
   - 每日 08:50 自動推播至 LINE，亦可透過 `run_morning_briefing.bat` 手動查閱。

2. **🏛️ 三大法人籌碼深度監控**：
   - **大盤資金動向**：爬取證交所與櫃買中心每日外資、投信、自營商買賣超金額（億元）。
   - **個股籌碼評級**：自動回溯聚合近 3 日外資與投信買賣超張數，標記「土洋同步」、「投信認養」或「外資大買」。
   - **籌碼防倒貨濾網**：若法人近 3 日連續出貨提款，盤中即使創高亦嚴格過濾不追高。
   - **法人低檔布局策略**：月線打底、法人暗中吸籌且盤中放量起漲標的即時捕捉。

3. **🎓 學生專屬精確交易計畫**：
   - **進場掛單區間**：現價至 +1.0% 限價單建議，防追高警戒（> +2.0% 勿追）。
   - **嚴格風控停損**：固定 -3.5% 停損價位精確計算。
   - **兩段式階梯停利**：
     - **TP1 (+6.0%)**：建議賣出一半持股，鎖住利潤並將停損拉至保本。
     - **TP2 (+10.0%)**：波段滿足點，全數獲利了結。

4. **📱 手機友善卡片式 LINE 推播**：
   - 寬鬆版面、清楚層級與表情符號指引，告別擁擠雜亂。
   - 支援 LINE Messaging API 官方 Bot 官方推播。

5. **⚡ 極速零延遲平行掃描**：
   - 使用 `ThreadPoolExecutor` 59 檔股票多執行緒平行運算，單次掃描僅約 1~2 秒。
   - U 型微笑曲線動態全日推估量能，精準捕捉主力盤中異動。

6. **⏰ Windows 工作排程器全自動運作**：
   - 每週一至週五 **08:50**：自動啟動晨報並進入 09:00 盤中高頻監控。
   - 每週一至週五 **14:30**：自動爬取證交所三大法人交易金額與策略結算日報。

---

## 模組結構

| 檔案 | 職責說明 |
| :--- | :--- |
| `pre_market_manager.py` | 美股四大指數、TSM/NVDA 與台灣期交所台指期夜盤行情整合 |
| `institutional_manager.py` | TWSE/TPEx 三大法人買賣金額、個股 3 日籌碼吸籌/出貨分析 |
| `strategy_engine.py` | 量價突破、月線起漲、法人布局策略與學生交易計畫研判 |
| `volume_estimator.py` | 台股 U 型微笑曲線全日成交量動態推算演算法 |
| `data_manager.py` | Google Sheets 59 檔同步、技術指標與籌碼快取、即時報價雙源容錯 |
| `alert_manager.py` | SQLite 持久化、當日去重防重複發送、手機友善 LINE 卡片派送 |
| `main.py` | 系統入口（整合晨報、盤中監控、14:30 盤後法人結算） |
| `setup_scheduler.py` | 一鍵註冊 Windows 工作排程器任務 |
| `test_system.py` | 10 項完整單元測試與整合驗證套件 (100% PASS) |

---

## 快速批次檔（一鍵點擊）

- `run_morning_briefing.bat`：一鍵發送今日開盤前晨報（美股＋台指期夜盤）。
- `start_market_monitor.bat`：啟動盤中即時監控（08:50 開啟）。
- `run_settle_1430.bat`：手動執行 14:30 盤後法人籌碼與策略結算。
- `setup_scheduler.bat`：一鍵設定 Windows 工作排程器。

---

## 終端機指令

- **盤中常駐即時監控**：
  ```bash
  python main.py
  ```

- **手動發送開盤前晨報**：
  ```bash
  python main.py --morning
  ```

- **手動執行 14:30 盤後法人結算**：
  ```bash
  python main.py --settle
  ```

- **測試 LINE 推播連線**：
  ```bash
  python main.py --test-line
  ```

- **執行完整自動化測試**：
  ```bash
  python test_system.py
  ```

---

## ☁️ 雲端部署指南 (無需開電腦，24/7 全天候運行)

本系統已完整容器化並支援各主流雲端平台部署：

### 方案 A：Zeabur / Render（最簡單推薦，5 分鐘免指令一鍵部署）
1. 將專案代碼推送到您的 GitHub 私有或公開倉庫。
2. 登入 [Zeabur](https://zeabur.com/) 或 [Render](https://render.com/)，點擊 **Create New Service** ➜ 選擇您的 GitHub 倉庫。
3. 系統會自動讀取 [`Dockerfile`](file:///c:/Users/88696/OneDrive/Desktop/程式測試/量化交易程式測試/Dockerfile) 並自動完成建置。
4. 在平台「環境變數（Environment Variables）」中加入：
   - `TZ`: `Asia/Taipei`
   - `LINE_CHANNEL_ACCESS_TOKEN`: 您的 LINE Bot Token
   - `LINE_USER_ID`: 您的 LINE User ID
5. 點擊部署完成！雲端容器將全天候 24 小時守護運行。

### 方案 B：輕量 Linux VPS 主機 (GCP / Oracle / Linode / DigitalOcean)
在任何 Linux 主機上，只需安裝 Docker 後輸入以下指令：
```bash
# 1. 啟動並在後台守護執行
docker compose up -d

# 2. 查看運行日誌
docker compose logs -f
```

### 方案 C：GitHub Actions (100% 完全免費 Serverless)
專案內已內建 [`.github/workflows/quant_daily.yml`](file:///c:/Users/88696/OneDrive/Desktop/程式測試/量化交易程式測試/.github/workflows/quant_daily.yml)：
1. 在 GitHub 倉庫的 **Settings ➜ Secrets and variables ➜ Actions** 加入：
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_USER_ID`
2. 每個交易日 08:50 自動推播晨報、14:30 自動推播三大法人結算，完全免主機費！

