"""
all_in_one_scanner.py
高併發台股量化監控系統 - 單一整合版 (All-in-One Standalone Edition)
包含：U型動態估量、雙軌策略、過熱濾網、風控停損、執行緒安全本地快取、多執行緒掃描與盤後結算
"""
import io
import os
import sys
import time
import json
import sqlite3
import logging
import threading
import requests
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import List, Dict, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf
import twstock

# ==========================================
# 1. 全域設定 (Configuration)
# ==========================================
@dataclass(frozen=True)
class Config:
    WATCHLIST_CSV_URLS: tuple = (
        "https://docs.google.com/spreadsheets/d/1nslH_nzxK5tB-EbwtBx52sMxiOyT2xQfa8b54GDi4ss/export?format=csv&gid=1312392024",
        "https://docs.google.com/spreadsheets/d/1nslH_nzxK5tB-EbwtBx52sMxiOyT2xQfa8b54GDi4ss/export?format=csv&gid=303711280",
    )
    WATCHLIST_CSV_URL: str = WATCHLIST_CSV_URLS[0]
    DEFAULT_WATCHLIST: tuple = ("2330", "2317", "2454", "2382", "3231", "2603", "2609", "3037")
    HTTP_TIMEOUT: int = 8
    MAX_WORKERS: int = 4
    SCAN_INTERVAL_SECONDS: int = 15
    QUOTE_CACHE_TTL_SECONDS: int = 15
    REQUEST_DELAY_SECONDS: float = 0.05
    BATCH_CHUNK_SIZE: int = 15
    BREAKOUT_PERIOD: int = 20
    VOLUME_BURST_MULTIPLE: float = 1.5
    MIN_AVG_VOLUME_LOTS: float = 500.0         # 指標2：最低流動性門檻 (>= 500張)
    STOP_LOSS_MIN_VOL_MULTIPLE: float = 1.0    # 指標1：帶量破線才停損 (>= 1.0倍均量)
    MAX_VOLUME_BURST_MULTIPLE: float = 3.5     # 指標3：爆天量防追高 (<= 3.5倍均量)
    REQUIRE_VOL_GOLDEN_CROSS: bool = True      # 指標4：均量黃金交叉 (MV5 > MV20)
    MAX_BIAS_MA20: float = 8.0
    STOP_LOSS_MA_TYPE: str = "MA20"
    # --- 學生專用：進場規劃與階梯停利參數 ---
    STOP_LOSS_PCT: float = 3.5        # 嚴格防守停損趴數 (-3.5%)
    TP1_PCT: float = 6.0              # 第一停利目標 TP1 (+6.0%，建議先出1/2保本)
    TP2_PCT: float = 10.0             # 第二停利目標 TP2 (+10.0%，波段滿足全數獲利了結)
    ENTRY_BUFFER_PCT: float = 1.0     # 建議進場掛單上限 (+1.0%)
    MAX_CHASE_PCT: float = 2.0        # 勿追高警戒線 (+2.0% 以上建議不追)
    LINE_NOTIFY_TOKEN: str = os.getenv("LINE_NOTIFY_TOKEN", "")
    LINE_CHANNEL_ACCESS_TOKEN: str = os.getenv(
        "LINE_CHANNEL_ACCESS_TOKEN", 
        "2Jh9ZcYXB8kAaQzFjcNCAImnvJs707XzXuGQOhaKEUK7QhihuqqjJwShoCv6RoYQ9PbBlAIxhQCVMSWq5o/sS4CPOLkp7x3lGuWYj63HmH4PWLhMSjUo+thw4NT6kJjBJ4qCtVTqguKl5tQaMNDfmQdB04t89/1O/w1cDnyilFU="
    )
    LINE_USER_ID: str = os.getenv("LINE_USER_ID", "U24a87027f5463870df6f01e223e3ac97")
    DB_PATH: str = "quant_history.db"
    KLINE_CACHE_DIR: str = "./cache_kline"

cfg = Config()

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TaiwanQuant")

# ==========================================
# 2. U型微笑曲線成交量估算引擎
# ==========================================
class VolumeEstimator:
    CUMULATIVE_BENCHMARKS = [
        (0,   0.00), (5,   0.08), (15,  0.18), (30,  0.28),
        (60,  0.40), (90,  0.50), (120, 0.58), (150, 0.65),
        (180, 0.72), (210, 0.80), (240, 0.88), (265, 0.94), (270, 1.00)
    ]

    @classmethod
    def get_market_elapsed_minutes(cls, now_dt: datetime) -> int:
        market_open = now_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        market_close = now_dt.replace(hour=13, minute=30, second=0, microsecond=0)
        if now_dt < market_open:
            return 0
        if now_dt >= market_close:
            return 270
        return int((now_dt - market_open).total_seconds() // 60)

    @classmethod
    def get_cumulative_ratio(cls, elapsed_mins: int) -> float:
        if elapsed_mins <= 0:
            return 0.05
        if elapsed_mins >= 270:
            return 1.00
        for i in range(len(cls.CUMULATIVE_BENCHMARKS) - 1):
            t1, r1 = cls.CUMULATIVE_BENCHMARKS[i]
            t2, r2 = cls.CUMULATIVE_BENCHMARKS[i + 1]
            if t1 <= elapsed_mins <= t2:
                return r1 + ((elapsed_mins - t1) / (t2 - t1)) * (r2 - r1)
        return 1.00

    @classmethod
    def estimate_full_day_volume(cls, current_volume: float, now_dt: datetime = None) -> Tuple[float, float]:
        if now_dt is None:
            now_dt = datetime.now()
        elapsed_mins = cls.get_market_elapsed_minutes(now_dt)
        ratio = max(cls.get_cumulative_ratio(elapsed_mins), 0.03)
        return round(current_volume / ratio, 2), round(ratio, 4)

# ==========================================
# 3. 數據管理器 (Data Manager)
# ==========================================
class DataManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._quote_lock = threading.Lock()
        self._daily_cache: Dict[str, dict] = {}
        self._quote_cache: Dict[str, tuple] = {}
        self._ticker_instances: Dict[str, Any] = {}
        self._last_kline_fetch_date: Optional[str] = None
        self._backoff_until: float = 0.0
        os.makedirs(cfg.KLINE_CACHE_DIR, exist_ok=True)

    def fetch_watchlist_from_google_sheets(self) -> List[str]:
        urls = list(getattr(cfg, "WATCHLIST_CSV_URLS", [cfg.WATCHLIST_CSV_URL]))
        combined_stocks: List[str] = []
        name_to_code = {v.name: k for k, v in twstock.codes.items()}
        aliases = {"合金": "6182", "合晶科技": "6182", "旭邦": "6576"}

        for url in urls:
            if not url or "YOUR_SHEET" in url:
                continue
            try:
                res = requests.get(url, timeout=cfg.HTTP_TIMEOUT)
                res.raise_for_status()
                res.encoding = 'utf-8'
                lines = [line.strip().replace('"', '') for line in res.text.splitlines() if line.strip()]
                for item in lines:
                    if not item or 'gid' in item.lower() or 'http' in item.lower():
                        continue
                    if item.isdigit() and len(item) in (4, 5):
                        combined_stocks.append(item)
                    elif item in name_to_code:
                        combined_stocks.append(name_to_code[item])
                    elif item in aliases:
                        combined_stocks.append(aliases[item])
                    else:
                        matches = [k for k, v in twstock.codes.items() if (item in v.name or v.name in item) and len(k) in (4, 5)]
                        if matches:
                            combined_stocks.append(matches[0])
            except Exception as e:
                logger.warning(f"讀取試算表失敗 [{url}]: {e}")

        unique_stocks = list(dict.fromkeys(combined_stocks))
        if unique_stocks:
            logger.info(f"成功從 Google Sheets 同步 {len(unique_stocks)} 檔股票名單 (含代碼與中文名稱自動解析)")
            return unique_stocks
        return list(cfg.DEFAULT_WATCHLIST)

    def _format_ticker(self, stock_id: str) -> str:
        try:
            info = twstock.codes.get(stock_id)
            if info and "上櫃" in getattr(info, "market", ""):
                return f"{stock_id}.TWO"
        except Exception:
            pass
        return f"{stock_id}.TW"

    def refresh_daily_indicators(self, stock_ids: List[str]) -> None:
        today_str = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            if self._last_kline_fetch_date == today_str and self._daily_cache:
                return

        logger.info(f"載入/計算 {len(stock_ids)} 檔標的之日K技術指標...")
        for sid in stock_ids:
            cache_file = os.path.join(cfg.KLINE_CACHE_DIR, f"{sid}_{today_str}.json")
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        with self._lock:
                            self._daily_cache[sid] = data
                        continue
                except Exception:
                    pass

            try:
                sym = self._format_ticker(sid)
                df = yf.Ticker(sym).history(period="6mo", timeout=cfg.HTTP_TIMEOUT)
                if df.empty or len(df) < 60:
                    continue
                close = df['Close']
                high = df['High']
                volume = df['Volume']
                
                ma20 = close.rolling(20).mean().iloc[-1]
                ma60 = close.rolling(60).mean().iloc[-1]
                high_20 = high.iloc[-20:].max()
                ma5_vol_lots = volume.rolling(5).mean().iloc[-1] / 1000.0
                ma20_vol_lots = volume.rolling(20).mean().iloc[-1] / 1000.0

                delta = close.diff()
                gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rs = gain / loss.replace(0, 0.00001)
                rsi_14 = (100 - (100 / (1 + rs))).iloc[-1]

                info = {
                    "stock_id": sid,
                    "symbol": sym,
                    "ma20": float(round(ma20, 2)),
                    "ma60": float(round(ma60, 2)),
                    "high_20": float(round(high_20, 2)),
                    "ma5_vol": float(round(ma5_vol_lots, 1)),
                    "ma20_vol": float(round(ma20_vol_lots, 1)),
                    "rsi_14": float(round(rsi_14, 2)),
                    "last_close": float(round(close.iloc[-1], 2))
                }
                with self._lock:
                    self._daily_cache[sid] = info
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"下載指標失敗 [{sid}]: {e}")

        with self._lock:
            self._last_kline_fetch_date = today_str

        # 預載三大法人近 3 日籌碼動向
        try:
            from institutional_manager import InstitutionalManager
            logger.info("開始載入三大法人近 3 日籌碼布局數據...")
            inst_data = InstitutionalManager.fetch_recent_accumulation(stock_ids, days=3)
            with self._lock:
                for sid, inst in inst_data.items():
                    if sid in self._daily_cache:
                        self._daily_cache[sid]["institutional"] = inst
            logger.info("三大法人籌碼數據載入完成！")
        except Exception as e:
            logger.warning(f"載入三大法人籌碼異常: {e}")

    def get_cached_indicators(self, stock_id: str) -> Optional[dict]:
        with self._lock:
            return self._daily_cache.get(stock_id)

    def fetch_realtime_quote(self, stock_id: str) -> Optional[dict]:
        now_ts = time.time()
        with self._quote_lock:
            if stock_id in self._quote_cache:
                cached_data, cached_ts = self._quote_cache[stock_id]
                if now_ts - cached_ts < getattr(cfg, "QUOTE_CACHE_TTL_SECONDS", 15):
                    return cached_data

        if now_ts < self._backoff_until:
            with self._quote_lock:
                if stock_id in self._quote_cache:
                    return self._quote_cache[stock_id][0]
            return None

        # 1. 嘗試 twstock
        try:
            raw = twstock.realtime.get(stock_id)
            if raw and raw.get('success'):
                rt = raw.get('realtime', {})
                p = rt.get('latest_trade_price')
                if p in (None, '-', '0.0'):
                    bids = rt.get('best_bid_price', [])
                    p = bids[0] if bids else None
                if p and p != '-':
                    quote = {
                        "stock_id": stock_id,
                        "current_price": float(p),
                        "accumulate_vol": float(rt.get('accumulate_trade_volume', 0)),
                        "timestamp": raw.get('info', {}).get('time', ''),
                        "source": "twstock"
                    }
                    with self._quote_lock:
                        self._quote_cache[stock_id] = (quote, now_ts)
                    return quote
        except Exception:
            pass

        # 2. 備援 yfinance fast_info (防限流連線池複用)
        try:
            delay = getattr(cfg, "REQUEST_DELAY_SECONDS", 0.05)
            if delay > 0:
                time.sleep(delay)

            with self._quote_lock:
                if stock_id not in self._ticker_instances:
                    sym = self._format_ticker(stock_id)
                    self._ticker_instances[stock_id] = yf.Ticker(sym)
                ticker = self._ticker_instances[stock_id]

            fi = ticker.fast_info
            p = getattr(fi, "last_price", None)
            v = getattr(fi, "last_volume", 0)
            if p and p > 0:
                quote = {
                    "stock_id": stock_id,
                    "current_price": float(round(p, 2)),
                    "accumulate_vol": float(round(v / 1000.0, 1)),
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "source": "yfinance_fast_info"
                }
                with self._quote_lock:
                    self._quote_cache[stock_id] = (quote, now_ts)
                return quote
        except Exception as e:
            err_msg = str(e).lower()
            if "429" in err_msg or "too many requests" in err_msg or "rate limit" in err_msg:
                self._backoff_until = now_ts + 10.0
                logger.warning("Yahoo 觸發頻率限制 (429)，自動啟動 10 秒冷卻保護...")
                with self._quote_lock:
                    if stock_id in self._quote_cache:
                        return self._quote_cache[stock_id][0]

        with self._quote_lock:
            if stock_id in self._quote_cache:
                return self._quote_cache[stock_id][0]
        return None

# ==========================================
# 4. 策略與風控引擎 (Strategy Engine)
# ==========================================
class StrategyEngine:
    @staticmethod
    def evaluate(stock_id: str, realtime: Dict[str, Any], daily_ind: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        price = realtime.get("current_price")
        current_vol = realtime.get("accumulate_vol", 0.0)
        if not price or price <= 0:
            return None

        code_info = twstock.codes.get(stock_id)
        stock_name = code_info.name if code_info else ""

        ma20 = daily_ind.get("ma20", 0.0)
        ma60 = daily_ind.get("ma60", 0.0)
        high_20 = daily_ind.get("high_20", 0.0)
        ma5_vol = daily_ind.get("ma5_vol", 0.0)
        ma20_vol = daily_ind.get("ma20_vol", 0.0)
        rsi_14 = daily_ind.get("rsi_14", 50.0)

        # 【指標2：最低流動性門檻】過濾冷凍殭屍股
        min_vol_lots = getattr(cfg, "MIN_AVG_VOLUME_LOTS", 500.0)
        if ma20_vol < min_vol_lots:
            return None

        est_vol, ratio = VolumeEstimator.estimate_full_day_volume(current_vol)
        vol_multiple = (est_vol / ma20_vol) if ma20_vol > 0 else 1.0
        bias_ma20 = ((price - ma20) / ma20) * 100.0 if ma20 > 0 else 0.0

        # 【指標1：帶量破線風控停損】需帶量才停損，無量跌破視為洗盤
        stop_loss_min_vol = getattr(cfg, "STOP_LOSS_MIN_VOL_MULTIPLE", 1.0)
        if cfg.STOP_LOSS_MA_TYPE == "MA20" and ma20 > 0 and price < ma20:
            if vol_multiple >= stop_loss_min_vol:
                return {
                    "stock_id": stock_id, "stock_name": stock_name, "type": "RISK_STOP_LOSS",
                    "title": "🚨 帶量跌破月線停損警報",
                    "desc": f"帶量跌破月線 MA20 ({ma20:.2f})！目前價: {price:.2f} 元 (放量 {vol_multiple:.2f}x)",
                    "price": price, "vol_multiple": round(vol_multiple, 2)
                }
        elif cfg.STOP_LOSS_MA_TYPE == "MA60" and ma60 > 0 and price < ma60:
            if vol_multiple >= stop_loss_min_vol:
                return {
                    "stock_id": stock_id, "stock_name": stock_name, "type": "RISK_STOP_LOSS",
                    "title": "🚨 帶量跌破季線停損警報",
                    "desc": f"帶量跌破季線 MA60 ({ma60:.2f})！目前價: {price:.2f} 元 (放量 {vol_multiple:.2f}x)",
                    "price": price, "vol_multiple": round(vol_multiple, 2)
                }

        # 過熱濾網
        if bias_ma20 > cfg.MAX_BIAS_MA20 or rsi_14 > cfg.MAX_RSI_14:
            return None

        # 【指標3：爆天量防追高】
        max_vol_burst = getattr(cfg, "MAX_VOLUME_BURST_MULTIPLE", 3.5)
        if vol_multiple > max_vol_burst:
            return None

        # 【指標4：均量黃金交叉確認】MV5 > MV20
        if getattr(cfg, "REQUIRE_VOL_GOLDEN_CROSS", True) and ma5_vol > 0 and ma20_vol > 0:
            if ma5_vol < ma20_vol:
                return None

        if ma20 > 0 and ma60 > 0 and ma20 < ma60 * 0.97:
            return None

        # --- 【法人籌碼濾網】：防主力誘多倒貨 ---
        inst = daily_ind.get("institutional", {})
        if inst.get("is_dumping", False):
            return None

        inst_tag = inst.get("tag", "")
        inst_suffix = f" ({inst_tag})" if inst_tag and "常態" not in inst_tag else ""

        # 策略一：創高突破
        if high_20 > 0 and price >= high_20 and vol_multiple >= cfg.VOLUME_BURST_MULTIPLE:
            stop_loss_pct = getattr(cfg, "STOP_LOSS_PCT", 3.5)
            tp1_pct = getattr(cfg, "TP1_PCT", 6.0)
            tp2_pct = getattr(cfg, "TP2_PCT", 10.0)
            entry_buf_pct = getattr(cfg, "ENTRY_BUFFER_PCT", 1.0)
            max_chase_pct = getattr(cfg, "MAX_CHASE_PCT", 2.0)

            entry_low = round(high_20, 2)
            entry_high = round(max(price, high_20) * (1 + entry_buf_pct / 100.0), 2)
            max_chase = round(max(price, high_20) * (1 + max_chase_pct / 100.0), 2)
            sl_price = round(price * (1 - stop_loss_pct / 100.0), 2)
            tp1_price = round(price * (1 + tp1_pct / 100.0), 2)
            tp2_price = round(price * (1 + tp2_pct / 100.0), 2)

            return {
                "stock_id": stock_id, "stock_name": stock_name, "type": "STRATEGY_BREAKOUT",
                "title": f"🚀 創高突破買進訊號{inst_suffix}",
                "desc": f"突破 20 日高點 ({high_20:.2f})！推算放量 {vol_multiple:.2f}x (全日估: {int(est_vol)}張)",
                "price": price, "bias": round(bias_ma20, 2), "rsi": round(rsi_14, 2),
                "vol_multiple": round(vol_multiple, 2), "est_vol": int(est_vol),
                "institutional": inst,
                "entry_plan": {
                    "entry_low": entry_low, "entry_high": entry_high, "max_chase": max_chase,
                    "stop_loss": sl_price, "stop_loss_pct": stop_loss_pct,
                    "tp1": tp1_price, "tp1_pct": tp1_pct, "tp2": tp2_price, "tp2_pct": tp2_pct
                }
            }

        # 策略二：月線起漲
        if 0.0 <= bias_ma20 <= 2.5 and vol_multiple >= (cfg.VOLUME_BURST_MULTIPLE * 0.9):
            stop_loss_pct = getattr(cfg, "STOP_LOSS_PCT", 3.5)
            tp1_pct = getattr(cfg, "TP1_PCT", 6.0)
            tp2_pct = getattr(cfg, "TP2_PCT", 10.0)
            entry_buf_pct = getattr(cfg, "ENTRY_BUFFER_PCT", 1.0)
            max_chase_pct = getattr(cfg, "MAX_CHASE_PCT", 2.0)

            entry_low = round(ma20, 2)
            entry_high = round(price * (1 + entry_buf_pct / 100.0), 2)
            max_chase = round(price * (1 + max_chase_pct / 100.0), 2)
            sl_price = round(min(price * (1 - stop_loss_pct / 100.0), ma20 * 0.985), 2)
            tp1_price = round(price * (1 + tp1_pct / 100.0), 2)
            tp2_price = round(price * (1 + tp2_pct / 100.0), 2)

            return {
                "stock_id": stock_id, "stock_name": stock_name, "type": "STRATEGY_MA20_REBOUND",
                "title": f"📈 月線起漲買進訊號{inst_suffix}",
                "desc": f"剛自月線起漲 (MA20: {ma20:.2f})，位階安全，放量 {vol_multiple:.2f}x",
                "price": price, "bias": round(bias_ma20, 2), "rsi": round(rsi_14, 2),
                "vol_multiple": round(vol_multiple, 2), "est_vol": int(est_vol),
                "institutional": inst,
                "entry_plan": {
                    "entry_low": entry_low, "entry_high": entry_high, "max_chase": max_chase,
                    "stop_loss": sl_price, "stop_loss_pct": stop_loss_pct,
                    "tp1": tp1_price, "tp1_pct": tp1_pct, "tp2": tp2_price, "tp2_pct": tp2_pct
                }
            }

        # 策略三：🏛️ 法人暗中布局起漲策略
        if inst.get("is_accumulating", False) and -0.5 <= bias_ma20 <= 3.0 and vol_multiple >= 1.2:
            stop_loss_pct = getattr(cfg, "STOP_LOSS_PCT", 3.5)
            tp1_pct = getattr(cfg, "TP1_PCT", 6.0)
            tp2_pct = getattr(cfg, "TP2_PCT", 10.0)
            entry_buf_pct = getattr(cfg, "ENTRY_BUFFER_PCT", 1.0)
            max_chase_pct = getattr(cfg, "MAX_CHASE_PCT", 2.0)

            entry_low = round(ma20, 2)
            entry_high = round(price * (1 + entry_buf_pct / 100.0), 2)
            max_chase = round(price * (1 + max_chase_pct / 100.0), 2)
            sl_price = round(min(price * (1 - stop_loss_pct / 100.0), ma20 * 0.98), 2)
            tp1_price = round(price * (1 + tp1_pct / 100.0), 2)
            tp2_price = round(price * (1 + tp2_pct / 100.0), 2)

            f_3d = inst.get("foreign_3d", 0.0)
            t_3d = inst.get("trust_3d", 0.0)
            return {
                "stock_id": stock_id, "stock_name": stock_name, "type": "STRATEGY_INST_ACCUMULATION",
                "title": f"🏛️ 法人暗中布局起漲訊號 ({inst_tag})",
                "desc": f"三大法人近3日默默布局 (外資 {f_3d:+.0f}張 / 投信 {t_3d:+.0f}張)！股價自月線帶量發動起漲！",
                "price": price, "bias": round(bias_ma20, 2), "rsi": round(rsi_14, 2),
                "vol_multiple": round(vol_multiple, 2), "est_vol": int(est_vol),
                "institutional": inst,
                "entry_plan": {
                    "entry_low": entry_low, "entry_high": entry_high, "max_chase": max_chase,
                    "stop_loss": sl_price, "stop_loss_pct": stop_loss_pct,
                    "tp1": tp1_price, "tp1_pct": tp1_pct, "tp2": tp2_price, "tp2_pct": tp2_pct
                }
            }

        return None

# ==========================================
# 5. 警報與持久化管理 (Alert Manager)
# ==========================================
class AlertManager:
    def __init__(self, db_path: str = cfg.DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._sent_today = set()
        self._init_db()

    def _init_db(self):
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute("""
                    CREATE TABLE IF NOT EXISTS alert_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        stock_id TEXT NOT NULL,
                        alert_type TEXT NOT NULL,
                        price REAL,
                        vol_multiple REAL,
                        message TEXT
                    )
                """)
                conn.commit()
                today = datetime.now().strftime("%Y-%m-%d")
                c.execute("SELECT stock_id, alert_type FROM alert_logs WHERE date = ?", (today,))
                for sid, atype in c.fetchall():
                    self._sent_today.add((str(sid), str(atype)))
                conn.close()
            except Exception as e:
                logger.error(f"SQLite 初始化失敗: {e}")

    def send_alert(self, signal: Dict[str, Any]) -> bool:
        sid = str(signal["stock_id"])
        atype = str(signal["type"])
        key = (sid, atype)

        with self._lock:
            if key in self._sent_today:
                return False
            self._sent_today.add(key)

        stock_name = signal.get("stock_name", "")
        if not stock_name:
            info = twstock.codes.get(sid)
            stock_name = info.name if info else ""
        name_str = f" {stock_name}" if stock_name else ""

        now_str = datetime.now().strftime("%H:%M:%S")
        price = float(signal.get('price', 0.0))
        est_vol = int(signal.get('est_vol', 0))
        vol_multiple = float(signal.get('vol_multiple', 1.0))
        plan = signal.get("entry_plan")

        if plan:
            inst = signal.get("institutional", {})
            inst_block = ""
            if inst and inst.get("tag") and "常態" not in inst.get("tag", ""):
                f_3d = inst.get("foreign_3d", 0.0)
                t_3d = inst.get("trust_3d", 0.0)
                tot_3d = inst.get("total_3d", 0.0)
                tag = inst.get("tag", "")
                inst_block = (
                    f"🏛️【法人籌碼布局監控】\n"
                    f"• 籌碼評估：{tag}\n"
                    f"• 投信動向：近3日 {t_3d:+.0f} 張\n"
                    f"• 外資動向：近3日 {f_3d:+.0f} 張\n"
                    f"• 法人合計：近3日 {tot_3d:+.0f} 張\n\n"
                )

            msg = (
                f"\n🔔【{signal['title']}】\n\n"
                f"📌 標的：{sid}{name_str}\n"
                f"• 即時現價：{price:.2f} 元\n"
                f"• 觸發時間：{now_str}\n\n"
                f"{inst_block}"
                f"🎯【建議進場掛單】\n"
                f"• 買進區間：{plan['entry_low']:.2f} ~ {plan['entry_high']:.2f} 元\n"
                f"• 追高警戒：高於 {plan['max_chase']:.2f} 元請勿追高\n\n"
                f"🛡️【學生保本與停利指引】\n"
                f"• 🛑 建議防守停損：{plan['stop_loss']:.2f} 元 (-{plan['stop_loss_pct']:.1f}%)\n"
                f"• 🎁 第一停利 TP1：{plan['tp1']:.2f} 元 (+{plan['tp1_pct']:.1f}%)\n"
                f"   ↳ 建議賣出 1/2 部位保本，剩餘持股零風險\n"
                f"• 🚀 第二停利 TP2：{plan['tp2']:.2f} 元 (+{plan['tp2_pct']:.1f}%)\n"
                f"   ↳ 波段滿足，全數獲利落袋\n\n"
                f"📊【量能與技術指標】\n"
                f"• 預估總量：{est_vol:,} 張 (放量 {vol_multiple:.1f}x)\n"
                f"• 月線乖離：{signal.get('bias', 0.0):+.2f}% ｜ RSI：{signal.get('rsi', 0.0):.1f}\n"
                f"• 訊號說明：{signal['desc']}"
            )
        else:
            msg = (
                f"\n⚠️【{signal['title']}】\n\n"
                f"📌 標的：{sid}{name_str}\n"
                f"• 即時現價：{price:.2f} 元\n"
                f"• 觸發時間：{now_str}\n\n"
                f"🚨【風控警示】\n"
                f"• 關鍵均線：{signal.get('ma_ref', 0.0):.2f} 元\n"
                f"• 處置建議：跌破關鍵防守線，建議分批減碼或停損，嚴控資金風險！\n\n"
                f"📊【量能狀態】\n"
                f"• 預估總量：{est_vol:,} 張 (放量 {vol_multiple:.1f}x)\n"
                f"• 訊號說明：{signal['desc']}"
            )

        self._persist_to_db(signal, msg)
        self.dispatch_line_message(msg)
        return True

    def _persist_to_db(self, signal: Dict[str, Any], msg: str):
        now = datetime.now()
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute("""
                    INSERT INTO alert_logs (date, timestamp, stock_id, alert_type, price, vol_multiple, message)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
                    str(signal["stock_id"]), str(signal["type"]),
                    float(signal.get("price", 0.0)), float(signal.get("vol_multiple", 0.0)), msg
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"寫入 SQLite 失敗: {e}")

        # LINE Messaging API Bot 推播 (支援特定 User 推播 或 全員廣播)
        if cfg.LINE_CHANNEL_ACCESS_TOKEN and not cfg.LINE_CHANNEL_ACCESS_TOKEN.startswith("YOUR_"):
            try:
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {cfg.LINE_CHANNEL_ACCESS_TOKEN}"
                }
                if cfg.LINE_USER_ID and not cfg.LINE_USER_ID.startswith("YOUR_"):
                    url = "https://api.line.me/v2/bot/message/push"
                    body = {
                        "to": cfg.LINE_USER_ID,
                        "messages": [{"type": "text", "text": message.strip()}]
                    }
                else:
                    url = "https://api.line.me/v2/bot/message/broadcast"
                    body = {
                        "messages": [{"type": "text", "text": message.strip()}]
                    }
                res = requests.post(url, headers=headers, json=body, timeout=cfg.HTTP_TIMEOUT)
                if res.status_code == 200:
                    logger.info("LINE Messaging API Bot 推播成功")
                    return True
                else:
                    logger.warning(f"LINE Bot 回應異常: {res.status_code} - {res.text}")
            except Exception as e:
                logger.error(f"LINE Bot 網路異常: {e}")

        logger.info(f"📢 [本地即時警報]{message}")
        return True

    def get_today_alerts(self) -> List[Dict[str, Any]]:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute("SELECT stock_id, alert_type, price, vol_multiple, timestamp FROM alert_logs WHERE date = ?", (today,))
                rows = c.fetchall()
                conn.close()
                return [{"stock_id": r[0], "alert_type": r[1], "price": r[2], "vol_multiple": r[3], "timestamp": r[4]} for r in rows]
            except Exception as e:
                logger.error(f"讀取警報記錄失敗: {e}")
                return []

# ==========================================
# 6. 多執行緒極速調度器 (Scanner)
# ==========================================
class ConcurrencyScanner:
    def __init__(self, data_mgr: DataManager, alert_mgr: AlertManager):
        self.data_mgr = data_mgr
        self.alert_mgr = alert_mgr

    def _scan_single_stock(self, stock_id: str):
        try:
            daily_ind = self.data_mgr.get_cached_indicators(stock_id)
            if not daily_ind:
                return
            realtime = self.data_mgr.fetch_realtime_quote(stock_id)
            if not realtime:
                return
            signal = StrategyEngine.evaluate(stock_id, realtime, daily_ind)
            if signal:
                self.alert_mgr.send_alert(signal)
        except Exception as e:
            logger.error(f"掃描標的異常 [{stock_id}]: {e}")

    def run_scan_cycle(self, stock_ids: List[str]) -> float:
        start_time = time.time()
        chunk_size = getattr(cfg, "BATCH_CHUNK_SIZE", 15)
        for i in range(0, len(stock_ids), chunk_size):
            chunk = stock_ids[i:i + chunk_size]
            with ThreadPoolExecutor(max_workers=cfg.MAX_WORKERS, thread_name_prefix="StockWorker") as executor:
                futures = [executor.submit(self._scan_single_stock, sid) for sid in chunk]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception:
                        pass
            if i + chunk_size < len(stock_ids):
                time.sleep(0.1)
        cost = time.time() - start_time
        logger.info(f"掃描完成：共 {len(stock_ids)} 檔標的，耗時 {cost:.2f} 秒")
        return cost

# ==========================================
# 7. 盤後結算與主循環 (Main Runner)
# ==========================================
def is_market_open_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return dtime(9, 0) <= now.time() <= dtime(13, 30)

def run_post_market_settlement(alert_mgr: AlertManager, data_mgr: DataManager):
    from institutional_manager import InstitutionalManager
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"========== 執行 {today} 14:30 盤後策略結算與三大法人籌碼作業 ==========")
    
    inst_market = InstitutionalManager.fetch_market_institutional_summary()
    alerts = alert_mgr.get_today_alerts()
    target_sids = [str(item["stock_id"]) for item in alerts]
    if not target_sids:
        target_sids = ["2330", "2317", "2454", "2603", "3037"]

    stock_flows = InstitutionalManager.fetch_stocks_institutional_flow(target_sids)

    inst_section_lines = []
    if inst_market:
        inst_section_lines = [
            "🏛️【今日三大法人交易金額】",
            f"• 🌐 外資及陸資：{inst_market['foreign_billion']:+.2f} 億元",
            f"• 💼 投信基金：{inst_market['trust_billion']:+.2f} 億元",
            f"• 🏢 自營商：{inst_market['dealer_billion']:+.2f} 億元",
            f"• 🎯 三大法人合計：{inst_market['total_billion']:+.2f} 億元\n",
            "────────────────────\n"
        ]

    if not alerts:
        report_lines = [f"\n📊【{today} 14:30 盤後法人籌碼與策略日報】\n"]
        report_lines.extend(inst_section_lines)
        report_lines.append("📋【今日策略狀態】\n今日無任何突破或停損訊號觸發，全數維持常態整理。")
        if stock_flows:
            report_lines.append("\n🔍【核心權值法人動向】")
            for sid in ["2330", "2317", "2454", "2603"]:
                flow = stock_flows.get(sid)
                if flow:
                    info = twstock.codes.get(sid)
                    sname = f" {info.name}" if info else ""
                    report_lines.append(f"• {sid}{sname}：外資 {flow['foreign_lots']:+.0f}張 | 投信 {flow['trust_lots']:+.0f}張")
        final_report = "\n".join(report_lines)
        logger.info(final_report)
        alert_mgr.dispatch_line_message(final_report)
        return

    pnl_list, wins = [], 0
    detail_lines = []

    zh_type_map = {
        "RISK_STOP_LOSS": "🚨 停損",
        "STRATEGY_BREAKOUT": "🚀 創高突破",
        "STRATEGY_MA20_REBOUND": "📈 月線起漲"
    }

    for idx, item in enumerate(alerts, 1):
        sid, signal_price, atype = item["stock_id"], item["price"], item["alert_type"]
        info = twstock.codes.get(sid)
        sname = f" {info.name}" if info else ""
        zh_type = zh_type_map.get(atype, atype)

        cached = data_mgr.get_cached_indicators(sid)
        close_price = cached.get("last_close", signal_price) if cached else signal_price
        pnl_pct = ((close_price - signal_price) / signal_price) * 100.0 if signal_price > 0 else 0.0
        pnl_list.append(pnl_pct)
        if pnl_pct >= 0:
            wins += 1
        sign = "+" if pnl_pct >= 0 else ""
        flow = stock_flows.get(sid)
        flow_str = ""
        if flow:
            flow_str = f"\n   籌碼: 外資 {flow['foreign_lots']:+.0f}張 | 投信 {flow['trust_lots']:+.0f}張"

        detail_lines.append(
            f"{idx}. {sid}{sname} [{zh_type}]\n"
            f"   進場: {signal_price:.2f} ➜ 結算: {close_price:.2f} ({sign}{pnl_pct:.2f}%){flow_str}"
        )

    total = len(pnl_list)
    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    avg_pnl = sum(pnl_list) / total if total > 0 else 0.0
    max_pnl = max(pnl_list) if pnl_list else 0.0
    min_pnl = min(pnl_list) if pnl_list else 0.0

    report_lines = [
        f"\n📊【{today} 14:30 盤後策略與法人結算日報】\n"
    ]
    report_lines.extend(inst_section_lines)
    report_lines.extend([
        "🏆【今日策略績效】",
        f"• 觸發標的：{total} 檔",
        f"• 🎯 策略勝率：{win_rate:.1f}% ({wins}/{total})",
        f"• 📈 平均損益：{avg_pnl:+.2f}%",
        f"• 🥇 最高表現：{max_pnl:+.2f}%",
        f"• 📉 最低表現：{min_pnl:+.2f}%\n",
        "────────────────────\n",
        "📋【各檔標的結算與籌碼明細】"
    ])
    report_lines.extend(detail_lines)

    final_report = "\n".join(report_lines)
    logger.info(final_report)
    alert_mgr.dispatch_line_message(final_report)

def run_pre_market_briefing(alert_mgr: AlertManager):
    """開盤前晨報作業 (美股四大指標、TSM/NVDA 與昨晚台指期夜盤)"""
    from pre_market_manager import PreMarketManager
    logger.info("========== 開始彙整並發送【台股開盤前晨報】==========")
    report = PreMarketManager.generate_pre_market_report()
    logger.info(report)
    success = alert_mgr.dispatch_line_message(report)
    if success:
        logger.info("✅ 開盤前晨報已成功發送至 LINE！")
    else:
        logger.error("❌ 開盤前晨報發送失敗。")
    logger.info("========== 開盤前晨報作業結束 ==========")

def main():
    args = sys.argv[1:]
    force_mode = "--force" in args
    once_mode = "--once" in args
    settle_mode = "--settle" in args
    morning_mode = "--morning" in args or "--briefing" in args
    test_line_mode = "--test-line" in args

    logger.info("高併發台股量化監控系統啟動...")
    data_mgr = DataManager()
    alert_mgr = AlertManager()
    scanner = ConcurrencyScanner(data_mgr, alert_mgr)

    if test_line_mode:
        logger.info("執行 LINE 推播連線測試...")
        success = alert_mgr.dispatch_line_message(
            "🔔【台股量化通報】連線測試成功！\n"
            "這是一則系統測試訊息，代表您的 LINE Bot 推播功能已正確連接！"
        )
        if success:
            logger.info("✅ LINE 測試訊息已成功送達！")
        return

    if morning_mode:
        run_pre_market_briefing(alert_mgr)
        return

    watchlist = data_mgr.fetch_watchlist_from_google_sheets()
    data_mgr.refresh_daily_indicators(watchlist)

    if settle_mode:
        run_post_market_settlement(alert_mgr, data_mgr)
        return
    if once_mode:
        scanner.run_scan_cycle(watchlist)
        return

    logger.info("進入常駐輪詢監控循環 (Ctrl+C 安全關閉)...")
    last_date = datetime.now().date()
    settlement_done = False
    morning_done = False

    # 開盤前時段啟動，自動發送晨報
    now_startup = datetime.now()
    if dtime(8, 0) <= now_startup.time() <= dtime(9, 5) and now_startup.weekday() < 5:
        logger.info("檢測到為開盤前時段啟動，自動發送【台股開盤前晨報】...")
        run_pre_market_briefing(alert_mgr)
        morning_done = True

    try:
        while True:
            now = datetime.now()
            if now.date() != last_date:
                last_date = now.date()
                settlement_done = False
                morning_done = False

            if force_mode or is_market_open_time():
                scanner.run_scan_cycle(watchlist)
                time.sleep(cfg.SCAN_INTERVAL_SECONDS)
                settlement_done = False
            else:
                if dtime(8, 45) <= now.time() < dtime(9, 0) and not morning_done and now.weekday() < 5:
                    run_pre_market_briefing(alert_mgr)
                    morning_done = True
                elif dtime(14, 30) <= now.time() <= dtime(14, 45) and not settlement_done and now.weekday() < 5:
                    run_post_market_settlement(alert_mgr, data_mgr)
                    settlement_done = True
                    time.sleep(300)
                else:
                    time.sleep(30)
    except KeyboardInterrupt:
        logger.info("系統安全退出。")

if __name__ == "__main__":
    main()
