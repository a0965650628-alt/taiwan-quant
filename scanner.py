"""
scanner.py
高併發調度器：使用 concurrent.futures.ThreadPoolExecutor 實現零延遲股票掃描
"""
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
from config import cfg
from data_manager import DataManager
from strategy_engine import StrategyEngine
from alert_manager import AlertManager

logger = logging.getLogger("Scanner")

class ConcurrencyScanner:
    def __init__(self, data_mgr: DataManager, alert_mgr: AlertManager):
        self.data_mgr = data_mgr
        self.alert_mgr = alert_mgr

    def _scan_single_stock(self, stock_id: str):
        """單一標的處理工作單元 (獨立執行緒執行)"""
        try:
            # 1. 取得該標的日K線技術指標快取 (線程安全)
            daily_ind = self.data_mgr.get_cached_indicators(stock_id)
            if not daily_ind:
                return

            # 2. 獲取盤中即時行情 (twstock 優先，yfinance fast_info 備援)
            realtime = self.data_mgr.fetch_realtime_quote(stock_id)
            if not realtime:
                return

            # 3. 執行策略研判、過熱濾網及風控檢驗
            signal = StrategyEngine.evaluate(stock_id, realtime, daily_ind)
            if signal:
                # 4. 發送警報 (包含當日去重與 LINE 派發)
                self.alert_mgr.send_alert(signal)

        except Exception as e:
            logger.error(f"掃描標的發生未預期異常 [{stock_id}]: {e}", exc_info=False)

    def run_scan_cycle(self, stock_ids: List[str]) -> float:
        """
        執行單輪多執行緒平行掃描 (平滑分批防限流版)
        :param stock_ids: 待掃描股票代碼清單
        :return: 本次掃描耗時 (秒)
        """
        start_time = time.time()
        chunk_size = getattr(cfg, "BATCH_CHUNK_SIZE", 15)
        
        # 分批平行處理，避免一次性突發數十個請求衝擊 Yahoo 或證交所伺服器
        for i in range(0, len(stock_ids), chunk_size):
            chunk = stock_ids[i:i + chunk_size]
            with ThreadPoolExecutor(max_workers=cfg.MAX_WORKERS, thread_name_prefix="StockWorker") as executor:
                futures = [executor.submit(self._scan_single_stock, sid) for sid in chunk]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Worker 執行緒異常: {e}")
            if i + chunk_size < len(stock_ids):
                time.sleep(0.1) # 批次間平滑微休息 (100ms)

        cost = time.time() - start_time
        logger.info(f"掃描輪迴完成：共 {len(stock_ids)} 檔標的，總耗時 {cost:.2f} 秒")
        return cost
