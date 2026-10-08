"""
test_system.py
系統全功能自動化驗證測試腳本 (單元測試 + 整合測試)
"""
import unittest
import os
import shutil
from datetime import datetime
from config import cfg
from volume_estimator import VolumeEstimator
from strategy_engine import StrategyEngine
from alert_manager import AlertManager
from data_manager import DataManager
from scanner import ConcurrencyScanner

class TestTaiwanQuantSystem(unittest.TestCase):
    def setUp(self):
        # 測試用臨時資料庫
        self.test_db = "test_quant.db"
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        self.alert_mgr = AlertManager(db_path=self.test_db)

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_volume_estimator(self):
        """測試 U 型微笑曲線成交量估算引擎"""
        # 09:15 測試
        dt_0915 = datetime(2026, 10, 7, 9, 15, 0)
        est_vol, ratio = VolumeEstimator.estimate_full_day_volume(1800, dt_0915)
        self.assertGreater(ratio, 0.15)
        self.assertLess(ratio, 0.25)
        self.assertGreater(est_vol, 5000)

        # 13:30 收盤測試
        dt_1330 = datetime(2026, 10, 7, 13, 30, 0)
        est_vol_close, ratio_close = VolumeEstimator.estimate_full_day_volume(10000, dt_1330)
        self.assertEqual(ratio_close, 1.0)
        self.assertEqual(est_vol_close, 10000.0)

    def test_strategy_breakout(self):
        """測試創高突破訊號研判 (含量能指標檢驗)"""
        daily_ind = {
            "ma20": 107.0,
            "ma60": 100.0,
            "high_20": 110.0,
            "ma5_vol": 1200.0,   # MV5 > MV20 (量能黃金交叉)
            "ma20_vol": 1000.0,  # 20日均量 1000 張 (> 500張流動性門檻)
            "rsi_14": 65.0,
            "last_close": 108.0
        }
        # 價格 111 (突破 20 日高點 110)，估量放大 (超過 1.5倍 且小於 3.5倍)
        realtime = {
            "current_price": 111.0,
            "accumulate_vol": 1600.0  # 1.6倍
        }
        signal = StrategyEngine.evaluate("2330", realtime, daily_ind)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["type"], "STRATEGY_BREAKOUT")

    def test_strategy_risk_stop_loss(self):
        """測試帶量跌破 MA20 停損風控"""
        daily_ind = {
            "ma20": 100.0,
            "ma60": 95.0,
            "high_20": 110.0,
            "ma5_vol": 1000.0,
            "ma20_vol": 1000.0, # 均量 1000 張
            "rsi_14": 40.0,
            "last_close": 99.0
        }
        # 帶量下殺：現價 97 (跌破 100) 且放量 1.2 倍
        realtime_heavy = {
            "current_price": 97.0,
            "accumulate_vol": 1200.0
        }
        signal_heavy = StrategyEngine.evaluate("2330", realtime_heavy, daily_ind)
        self.assertIsNotNone(signal_heavy, "帶量跌破應觸發停損")
        self.assertEqual(signal_heavy["type"], "RISK_STOP_LOSS")

        # 無量微跌：現價 97 但成交量僅 0.3 倍均量 (洗盤不警報)
        realtime_light = {
            "current_price": 97.0,
            "accumulate_vol": 300.0
        }
        signal_light = StrategyEngine.evaluate("2330", realtime_light, daily_ind)
        self.assertIsNone(signal_light, "無量跌破應被過濾，不發停損警報")

    def test_liquidity_filter(self):
        """測試指標2：最低流動性門檻 (均量 < 500 張直接過濾)"""
        daily_ind = {
            "ma20": 100.0,
            "ma60": 95.0,
            "high_20": 105.0,
            "ma5_vol": 200.0,
            "ma20_vol": 150.0, # 均量僅 150 張 (低於 500 張)
            "rsi_14": 55.0,
            "last_close": 106.0
        }
        realtime = {"current_price": 108.0, "accumulate_vol": 500.0}
        signal = StrategyEngine.evaluate("2330", realtime, daily_ind)
        self.assertIsNone(signal, "流動性不足的冷門股應被過濾")

    def test_volume_blowoff_filter(self):
        """測試指標3：爆天量防追高 (預估量 > 3.5 倍均量不追高)"""
        daily_ind = {
            "ma20": 107.0,
            "ma60": 100.0,
            "high_20": 110.0,
            "ma5_vol": 1500.0,
            "ma20_vol": 1000.0,
            "rsi_14": 65.0,
            "last_close": 108.0
        }
        # 爆天量 4.0 倍 (> 3.5 倍)
        realtime = {"current_price": 111.0, "accumulate_vol": 4000.0}
        signal = StrategyEngine.evaluate("2330", realtime, daily_ind)
        self.assertIsNone(signal, "爆天量主力出貨可能，應被過濾不追高")

    def test_volume_golden_cross_filter(self):
        """測試指標4：均量黃金交叉 (MV5 < MV20 均量退潮不進場)"""
        daily_ind = {
            "ma20": 107.0,
            "ma60": 100.0,
            "high_20": 110.0,
            "ma5_vol": 800.0,   # 5日均量 800 < 20日均量 1000
            "ma20_vol": 1000.0,
            "rsi_14": 65.0,
            "last_close": 108.0
        }
        realtime = {"current_price": 111.0, "accumulate_vol": 1600.0}
        signal = StrategyEngine.evaluate("2330", realtime, daily_ind)
        self.assertIsNone(signal, "均量尚未黃金交叉時不應觸發進場")

    def test_alert_manager_deduplication(self):
        """測試警報去重防止重複發送"""
        signal = {
            "stock_id": "2330",
            "type": "STRATEGY_BREAKOUT",
            "title": "測試創高",
            "desc": "測試說明",
            "price": 1000.0,
            "vol_multiple": 2.0
        }
        # 第一次發送應成功
        res1 = self.alert_mgr.send_alert(signal)
        self.assertTrue(res1)

        # 第二次發送相同標的與策略應被去重阻擋
        res2 = self.alert_mgr.send_alert(signal)
        self.assertFalse(res2)

    def test_student_trading_plan(self):
        """測試學生專用進場規劃、停損與階梯停利目標計算"""
        daily_ind = {
            "ma20": 100.0,
            "ma60": 95.0,
            "high_20": 105.0,
            "ma5_vol": 1200.0,
            "ma20_vol": 1000.0,
            "rsi_14": 60.0,
            "last_close": 104.0
        }
        realtime = {
            "current_price": 106.0,
            "accumulate_vol": 1600.0
        }
        signal = StrategyEngine.evaluate("2330", realtime, daily_ind)
        self.assertIsNotNone(signal)
        self.assertIn("entry_plan", signal)
        
        plan = signal["entry_plan"]
        # 進場低點應為突破點 105.0
        self.assertEqual(plan["entry_low"], 105.0)
        # 停損應為 106.0 * (1 - 0.035) = 102.29
        self.assertEqual(plan["stop_loss"], round(106.0 * 0.965, 2))
        # TP1 應為 106.0 * 1.06 = 112.36
        self.assertEqual(plan["tp1"], round(106.0 * 1.06, 2))
        # TP2 應為 106.0 * 1.10 = 116.60
        self.assertEqual(plan["tp2"], round(106.0 * 1.10, 2))

        # 測試推播訊息格式化 (驗證手機卡片排版無拋錯)
        res = self.alert_mgr.send_alert(signal)
        self.assertTrue(res)

    def test_institutional_accumulation_and_dumping(self):
        """測試三大法人籌碼暗中布局策略與防倒貨濾網"""
        daily_ind_dump = {
            "ma20": 100.0,
            "ma60": 95.0,
            "high_20": 105.0,
            "ma5_vol": 1200.0,
            "ma20_vol": 1000.0,
            "rsi_14": 60.0,
            "last_close": 104.0,
            "institutional": {
                "foreign_3d": -1200.0,
                "trust_3d": -300.0,
                "total_3d": -1500.0,
                "is_accumulating": False,
                "is_dumping": True,
                "tag": "⚠️ 法人連日倒貨提款 (-1500張)"
            }
        }
        realtime_breakout = {"current_price": 106.0, "accumulate_vol": 1600.0}
        # 法人大倒貨時，即使帶量突破 20 日高，亦應被濾除
        sig_dump = StrategyEngine.evaluate("2330", realtime_breakout, daily_ind_dump)
        self.assertIsNone(sig_dump, "法人大舉出貨時應被過濾，不追高買進")

        # 法人暗中布局股：股價在月線 (100.5)，放量 1.3 倍，法人近 3 日大買
        daily_ind_accum = {
            "ma20": 100.0,
            "ma60": 95.0,
            "high_20": 115.0,
            "ma5_vol": 1200.0,
            "ma20_vol": 1000.0,
            "rsi_14": 52.0,
            "last_close": 99.8,
            "institutional": {
                "foreign_3d": 6500.0,
                "trust_3d": 800.0,
                "total_3d": 7300.0,
                "is_accumulating": True,
                "is_dumping": False,
                "tag": "🏛️ 土洋同步聯手布局"
            }
        }
        realtime_rebound = {"current_price": 101.0, "accumulate_vol": 1300.0}
        sig_accum = StrategyEngine.evaluate("2330", realtime_rebound, daily_ind_accum)
        self.assertIsNotNone(sig_accum, "法人默默布局且月線帶量起漲應觸發訊號")
        self.assertIn("institutional", sig_accum)
        self.assertIn("土洋同步", sig_accum["title"])

        # 驗證推播格式化 (含籌碼資訊) 無報錯
        sent = self.alert_mgr.send_alert(sig_accum)
        self.assertTrue(sent)

    def test_pre_market_manager(self):
        """測試開盤前晨報模組 (美股四大指標與台指期夜盤格式化)"""
        from pre_market_manager import PreMarketManager
        report = PreMarketManager.generate_pre_market_report()
        self.assertIsInstance(report, str)
        self.assertIn("台股開盤前晨報", report)
        self.assertIn("美股四大指數", report)
        self.assertIn("昨晚台指期夜盤", report)

if __name__ == "__main__":
    unittest.main()
