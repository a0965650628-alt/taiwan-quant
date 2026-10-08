"""
data_manager.py
數據管理模組：Google Sheets Watchlist 動態同步、yfinance 日K技術指標快取、即時行情 (twstock + yfinance 雙引擎容錯)
"""
import io
import os
import json
import logging
import threading
import time
import requests
import pandas as pd
import yfinance as yf
import twstock
from typing import List, Dict, Optional, Any
from datetime import datetime
from config import cfg

logger = logging.getLogger("DataManager")

class DataManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._quote_lock = threading.Lock()
        self._daily_cache: Dict[str, dict] = {} # 技術指標快取
        self._quote_cache: Dict[str, tuple] = {} # 即時報價記憶體快取 (data, timestamp)
        self._ticker_instances: Dict[str, Any] = {} # 複用 Ticker 連線物件 (避免重複建立 SSL/HTTP 連線)
        self._last_kline_fetch_date: Optional[str] = None
        self._backoff_until: float = 0.0 # 觸發限流時之冷卻時間戳
        os.makedirs(cfg.KLINE_CACHE_DIR, exist_ok=True)

    def fetch_watchlist_from_google_sheets(self) -> List[str]:
        """
        從 Google Sheets CSV 動態讀取股票代碼清單
        支援：多試算表/多分頁 (gid)、數字代碼 (2330) 與 中文股票名稱 (台特化、家登等自動轉譯為代碼)
        """
        urls = list(getattr(cfg, "WATCHLIST_CSV_URLS", [cfg.WATCHLIST_CSV_URL]))
        combined_stocks: List[str] = []
        
        # 建立中文名稱對應代碼字典 (含常見簡稱別名)
        name_to_code = {v.name: k for k, v in twstock.codes.items()}
        aliases = {"合金": "6182", "合晶科技": "6182", "旭邦": "6576"}

        for url in urls:
            if not url or "YOUR_SHEET_ID" in url:
                continue
            try:
                res = requests.get(url, timeout=cfg.HTTP_TIMEOUT)
                res.raise_for_status()
                res.encoding = 'utf-8'
                
                lines = [line.strip().replace('"', '') for line in res.text.splitlines() if line.strip()]
                for item in lines:
                    if not item or 'gid' in item.lower() or 'http' in item.lower():
                        continue
                    
                    # 1. 直接是 4~5 位數字代碼
                    if item.isdigit() and len(item) in (4, 5):
                        combined_stocks.append(item)
                    # 2. 完全匹配中文股票名稱
                    elif item in name_to_code:
                        combined_stocks.append(name_to_code[item])
                    # 3. 常見別名
                    elif item in aliases:
                        combined_stocks.append(aliases[item])
                    # 4. 模糊匹配 (例如: '華邦電' 匹配 '華邦電子')
                    else:
                        matches = [k for k, v in twstock.codes.items() if (item in v.name or v.name in item) and len(k) in (4, 5)]
                        if matches:
                            combined_stocks.append(matches[0])
            except Exception as e:
                logger.warning(f"讀取試算表分頁失敗 [{url}]: {e}")

        # 去除重複項並保持原始排序
        unique_stocks = list(dict.fromkeys(combined_stocks))
        
        if unique_stocks:
            logger.info(f"成功從 Google Sheets 同步 {len(unique_stocks)} 檔股票名單 (含代碼與中文名稱自動解析)")
            return unique_stocks

        logger.info("未獲取到有效標的，切換為預設監控清單")
        return list(cfg.DEFAULT_WATCHLIST)

    def _format_ticker(self, stock_id: str) -> str:
        """判斷上市(.TW)或上櫃(.TWO)格式，支援自動容錯轉譯"""
        try:
            info = twstock.codes.get(stock_id)
            if info and "上櫃" in getattr(info, "market", ""):
                return f"{stock_id}.TWO"
            if info and "上市" in getattr(info, "market", ""):
                return f"{stock_id}.TW"
        except Exception:
            pass
        # 7 開頭或 6 開頭在台股常為上櫃/戰略新板標的 (如 7856 台特化)
        if stock_id.startswith(("7", "6")):
            return f"{stock_id}.TWO"
        return f"{stock_id}.TW"

    def refresh_daily_indicators(self, stock_ids: List[str]) -> None:
        """
        每日盤前或開盤計算日K指標 (MA20, MA60, 20日高, 14日RSI, 20日均量)
        並寫入本地快取，支援新上市股自適應均線與雙市場尾綴備援
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            if self._last_kline_fetch_date == today_str and self._daily_cache:
                return

        logger.info(f"開始載入/計算 {len(stock_ids)} 檔標的之日K技術指標...")
        
        for sid in stock_ids:
            try:
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

                ticker_sym = self._format_ticker(sid)
                ticker = yf.Ticker(ticker_sym)
                df = ticker.history(period="6mo", timeout=cfg.HTTP_TIMEOUT)
                
                # 雙向容錯：若預設後綴無資料，自動切換上市/櫃後綴嘗試
                if df.empty:
                    alt_sym = f"{sid}.TW" if ticker_sym.endswith(".TWO") else f"{sid}.TWO"
                    alt_df = yf.Ticker(alt_sym).history(period="6mo", timeout=cfg.HTTP_TIMEOUT)
                    if not alt_df.empty:
                        ticker_sym = alt_sym
                        df = alt_df

                if df.empty or len(df) < 20:
                    logger.warning(f"標的 [{sid}] 日K資料不足 20 天，無法計算基準指標")
                    continue
                
                close = df['Close']
                high = df['High']
                volume = df['Volume']
                
                # 自適應均線：若上市未滿 60 天但已滿 20 天 (新掛牌強勢股)，動態支援
                ma20 = close.rolling(window=20).mean().iloc[-1]
                if len(df) >= 60:
                    ma60 = close.rolling(window=60).mean().iloc[-1]
                else:
                    ma60 = ma20  # 新股以月線替代季線作為保護
                    logger.info(f"標的 [{sid}] 為新上市/櫃股 (掛牌 {len(df)} 天)，啟動自適應 MA20 監控")
                
                high_20 = high.iloc[-20:].max()
                
                # yfinance volume 單位為股 (Shares)，轉換為張 (Lots) 以匹配盤中報價
                ma5_vol_shares = volume.rolling(window=5).mean().iloc[-1]
                ma5_vol_lots = ma5_vol_shares / 1000.0
                ma20_vol_shares = volume.rolling(window=20).mean().iloc[-1]
                ma20_vol_lots = ma20_vol_shares / 1000.0
                
                # 計算 RSI 14
                delta = close.diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss.replace(0, 0.00001)
                rsi_14 = (100 - (100 / (1 + rs))).iloc[-1]

                indicator_data = {
                    "stock_id": sid,
                    "symbol": ticker_sym,
                    "ma20": float(round(ma20, 2)),
                    "ma60": float(round(ma60, 2)),
                    "high_20": float(round(high_20, 2)),
                    "ma5_vol": float(round(ma5_vol_lots, 1)),   # 5日均量 (張)
                    "ma20_vol": float(round(ma20_vol_lots, 1)), # 20日均量 (張)
                    "rsi_14": float(round(rsi_14, 2)),
                    "last_close": float(round(close.iloc[-1], 2))
                }

                with self._lock:
                    self._daily_cache[sid] = indicator_data

                # 保存至本機快取
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(indicator_data, f, ensure_ascii=False, indent=2)

            except Exception as e:
                logger.error(f"下載或計算日K指標失敗 [{sid}]: {e}")

        with self._lock:
            self._last_kline_fetch_date = today_str
        logger.info(f"技術指標載入完成，共計緩存 {len(self._daily_cache)} 檔標的")

        # 3. 預載三大法人近 3 日籌碼動向 (外資、投信累計買賣超與布局標記)
        try:
            from institutional_manager import InstitutionalManager
            logger.info("開始載入/計算 59 檔標的之三大法人近 3 日籌碼布局數據...")
            inst_data = InstitutionalManager.fetch_recent_accumulation(stock_ids, days=3)
            with self._lock:
                for sid, inst in inst_data.items():
                    if sid in self._daily_cache:
                        self._daily_cache[sid]["institutional"] = inst
            logger.info(f"三大法人近 3 日籌碼數據載入完成，共計標記 {len(inst_data)} 檔標的")
        except Exception as e:
            logger.warning(f"載入三大法人籌碼數據異常: {e}")

    def get_cached_indicators(self, stock_id: str) -> Optional[dict]:
        """執行緒安全地取得單一標的指標快取"""
        with self._lock:
            return self._daily_cache.get(stock_id)

    def fetch_realtime_quote(self, stock_id: str) -> Optional[dict]:
        """
        抓取即時報價 (防限流增強版)：
        1. 優先檢查記憶體快取 (TTL 內直接回傳，完全不發送網路請求)
        2. 嘗試 twstock 即時行情
        3. 備援 yfinance fast_info：複用 Ticker 物件連線、微延遲防爆量，遇 429 自動退避冷卻
        """
        now_ts = time.time()
        
        # 1. 優先命中記憶體快取 (15 秒內不重複發送請求)
        with self._quote_lock:
            if stock_id in self._quote_cache:
                cached_data, cached_ts = self._quote_cache[stock_id]
                if now_ts - cached_ts < getattr(cfg, "QUOTE_CACHE_TTL_SECONDS", 15):
                    return cached_data

        # 若目前正處於 Yahoo 限流冷卻期，直接回傳舊快取 (若有的話)
        if now_ts < self._backoff_until:
            with self._quote_lock:
                if stock_id in self._quote_cache:
                    return self._quote_cache[stock_id][0]
            return None

        # 2. 嘗試 twstock.realtime
        try:
            raw = twstock.realtime.get(stock_id)
            if raw and raw.get('success'):
                realtime_info = raw.get('realtime', {})
                latest_trade_price = realtime_info.get('latest_trade_price')
                
                if latest_trade_price in (None, '-', '0.0'):
                    bids = realtime_info.get('best_bid_price', [])
                    latest_trade_price = bids[0] if bids else None
                
                if latest_trade_price and latest_trade_price != '-':
                    quote = {
                        "stock_id": stock_id,
                        "current_price": float(latest_trade_price),
                        "accumulate_vol": float(realtime_info.get('accumulate_trade_volume', 0)),
                        "timestamp": raw.get('info', {}).get('time', ''),
                        "source": "twstock"
                    }
                    with self._quote_lock:
                        self._quote_cache[stock_id] = (quote, now_ts)
                    return quote
        except Exception:
            pass

        # 3. 高可用備援：yfinance fast_info (防限流連線池複用)
        try:
            # 請求間微延遲 (預設 50ms，避免同毫秒併發高流量衝擊 Yahoo WAF)
            delay = getattr(cfg, "REQUEST_DELAY_SECONDS", 0.05)
            if delay > 0:
                time.sleep(delay)

            # 複用 Ticker 連線個體 (Keep-Alive 連線池)
            with self._quote_lock:
                if stock_id not in self._ticker_instances:
                    ticker_sym = self._format_ticker(stock_id)
                    self._ticker_instances[stock_id] = yf.Ticker(ticker_sym)
                ticker = self._ticker_instances[stock_id]

            fi = ticker.fast_info
            price = getattr(fi, "last_price", None)
            vol_shares = getattr(fi, "last_volume", 0)
            
            if price is not None and price > 0:
                quote = {
                    "stock_id": stock_id,
                    "current_price": float(round(price, 2)),
                    "accumulate_vol": float(round(vol_shares / 1000.0, 1)),
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
                logger.warning(f"Yahoo 觸發頻率限制 (429)，自動啟動 10 秒冷卻保護...")
                with self._quote_lock:
                    if stock_id in self._quote_cache:
                        return self._quote_cache[stock_id][0]
            logger.debug(f"即時報價雙源皆無法獲取 [{stock_id}]: {e}")

        # 容錯：若失敗但之前有快取，回傳舊快取抗崩潰
        with self._quote_lock:
            if stock_id in self._quote_cache:
                return self._quote_cache[stock_id][0]

        return None
