"""
config.py
高併發台股量化監控系統 - 全域設定檔
"""
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    # --- 數據源配置 ---
    # Google Sheets CSV 發佈連結清單 (支援多分頁 gid 動態載入)
    WATCHLIST_CSV_URLS: tuple = (
        "https://docs.google.com/spreadsheets/d/1nslH_nzxK5tB-EbwtBx52sMxiOyT2xQfa8b54GDi4ss/export?format=csv&gid=1312392024",
        "https://docs.google.com/spreadsheets/d/1nslH_nzxK5tB-EbwtBx52sMxiOyT2xQfa8b54GDi4ss/export?format=csv&gid=303711280",
    )
    WATCHLIST_CSV_URL: str = WATCHLIST_CSV_URLS[0]
    
    # 預設本地股票名單（當無法連線 Google Sheets 時的 Fallback 備援）
    DEFAULT_WATCHLIST: tuple = ("2330", "2317", "2454", "2382", "3231", "2603", "2609", "3037")
    
    # 網路請求 Timeout (秒)
    HTTP_TIMEOUT: int = 8
    
    # --- 併發與防限流排程 ---
    MAX_WORKERS: int = 4               # 併發執行緒數 (降至 4 可避免觸發 Yahoo IP 限流)
    SCAN_INTERVAL_SECONDS: int = 15    # 盤中掃描週期 (15秒為兼顧即時性與防封鎖之最佳頻率)
    QUOTE_CACHE_TTL_SECONDS: int = 15  # 即時報價記憶體快取秒數 (防短時間重複請求)
    REQUEST_DELAY_SECONDS: float = 0.05 # 標的請求間微延遲 (秒)
    BATCH_CHUNK_SIZE: int = 15         # 批次分組大小 (避免一次併發數十筆請求)
    
    # --- 雙軌策略參數 ---
    BREAKOUT_PERIOD: int = 20      # 創高週期（20日新高）
    VOLUME_BURST_MULTIPLE: float = 1.5 # 估量放量倍數門檻 (估量 / 20MA量 >= 1.5倍)
    
    # --- 4項進階成交量指標與濾網 ---
    MIN_AVG_VOLUME_LOTS: float = 500.0         # 指標2：最低流動性門檻 (20日均量需 >= 500張，過濾冷凍殭屍股)
    STOP_LOSS_MIN_VOL_MULTIPLE: float = 1.0    # 指標1：帶量破線才停損 (預估量需 >= 1.0倍均量，無量跌破視為洗盤不警報)
    MAX_VOLUME_BURST_MULTIPLE: float = 3.5     # 指標3：爆天量防追高 (預估量 > 3.5倍均量視為出貨不追高)
    REQUIRE_VOL_GOLDEN_CROSS: bool = True      # 指標4：均量黃金交叉 (5日均量 MV5 > 20日均量 MV20 才進場)
    
    # --- 過熱濾網參數 ---
    MAX_BIAS_MA20: float = 8.0     # 月線正乖離率上限 (8% 防追高)
    MAX_RSI_14: float = 75.0       # RSI-14 上限 (防極端過熱)
    
    # --- 風控停損參數 ---
    STOP_LOSS_MA_TYPE: str = "MA20" # 風控破線基準 ("MA20" 或 "MA60")
    
    # --- 學生專用：進場規劃與階梯停利參數 ---
    STOP_LOSS_PCT: float = 3.5        # 嚴格防守停損趴數 (-3.5%)
    TP1_PCT: float = 6.0              # 第一停利目標 TP1 (+6.0%，建議先出1/2保本)
    TP2_PCT: float = 10.0             # 第二停利目標 TP2 (+10.0%，波段滿足全數獲利了結)
    ENTRY_BUFFER_PCT: float = 1.0     # 建議進場掛單上限 (+1.0%)
    MAX_CHASE_PCT: float = 2.0        # 勿追高警戒線 (+2.0% 以上建議不追)
    
    # --- LINE 推播設定 ---
    # 支援 LINE Notify Token 或 LINE Messaging API Channel Token
    LINE_NOTIFY_TOKEN: str = os.getenv("LINE_NOTIFY_TOKEN", "")
    LINE_CHANNEL_ACCESS_TOKEN: str = os.getenv(
        "LINE_CHANNEL_ACCESS_TOKEN", 
        "2Jh9ZcYXB8kAaQzFjcNCAImnvJs707XzXuGQOhaKEUK7QhihuqqjJwShoCv6RoYQ9PbBlAIxhQCVMSWq5o/sS4CPOLkp7x3lGuWYj63HmH4PWLhMSjUo+thw4NT6kJjBJ4qCtVTqguKl5tQaMNDfmQdB04t89/1O/w1cDnyilFU="
    )
    LINE_USER_ID: str = os.getenv("LINE_USER_ID", "U24a87027f5463870df6f01e223e3ac97")
    
    # --- 快取與持久化 ---
    DB_PATH: str = "quant_history.db"
    KLINE_CACHE_DIR: str = "./cache_kline"

cfg = Config()
