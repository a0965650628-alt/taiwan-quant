"""
strategy_engine.py
量化策略引擎：雙軌策略 (創高突破 / 月線起漲)、過熱濾網 (Bias/RSI)、破線停損風控
"""
import logging
import twstock
from typing import Dict, Any, Optional
from config import cfg
from volume_estimator import VolumeEstimator

logger = logging.getLogger("StrategyEngine")

class StrategyEngine:
    @staticmethod
    def evaluate(stock_id: str, realtime: Dict[str, Any], daily_ind: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        核心訊號評估邏輯
        :param stock_id: 股票代號 (例: "2330")
        :param realtime: 即時行情快照 (current_price, accumulate_vol 等)
        :param daily_ind: 日K技術指標 (ma20, ma60, high_20, ma20_vol, rsi_14, last_close)
        :return: 觸發的訊號資訊字典，若無觸發則返回 None
        """
        price = realtime.get("current_price")
        current_vol = realtime.get("accumulate_vol", 0.0)
        
        if price is None or price <= 0:
            return None

        # 查詢公司繁體中文名稱
        code_info = twstock.codes.get(stock_id)
        stock_name = code_info.name if code_info else ""

        ma20 = daily_ind.get("ma20", 0.0)
        ma60 = daily_ind.get("ma60", 0.0)
        high_20 = daily_ind.get("high_20", 0.0)
        ma5_vol = daily_ind.get("ma5_vol", 0.0)
        ma20_vol = daily_ind.get("ma20_vol", 0.0)
        rsi_14 = daily_ind.get("rsi_14", 50.0)
        last_close = daily_ind.get("last_close", price)

        # 【指標2：最低流動性濾網】過濾 20 日均量低於門檻之冷凍殭屍股
        min_vol_lots = getattr(cfg, "MIN_AVG_VOLUME_LOTS", 500.0)
        if ma20_vol < min_vol_lots:
            return None

        # 1. 動態推算全日成交量與放量倍數 (U型微笑曲線)
        est_vol, ratio = VolumeEstimator.estimate_full_day_volume(current_vol)
        vol_multiple = (est_vol / ma20_vol) if ma20_vol > 0 else 1.0

        # 2. 計算 20MA 正負乖離率 (%)
        bias_ma20 = ((price - ma20) / ma20) * 100.0 if ma20 > 0 else 0.0

        # --- 【指標1：帶量破線風控停損】需帶量下殺才停損，無量微跌視為洗盤不警報 ---
        stop_loss_min_vol = getattr(cfg, "STOP_LOSS_MIN_VOL_MULTIPLE", 1.0)
        if cfg.STOP_LOSS_MA_TYPE == "MA20" and ma20 > 0 and price < ma20:
            if vol_multiple >= stop_loss_min_vol:
                return {
                    "stock_id": stock_id,
                    "stock_name": stock_name,
                    "type": "RISK_STOP_LOSS",
                    "title": "🚨 帶量跌破月線停損警報",
                    "desc": f"帶量跌破月線 MA20 ({ma20:.2f})！目前價: {price:.2f} 元 (放量 {vol_multiple:.2f}x)",
                    "price": price,
                    "ma_ref": ma20,
                    "vol_multiple": round(vol_multiple, 2),
                    "est_vol": int(est_vol)
                }
        elif cfg.STOP_LOSS_MA_TYPE == "MA60" and ma60 > 0 and price < ma60:
            if vol_multiple >= stop_loss_min_vol:
                return {
                    "stock_id": stock_id,
                    "stock_name": stock_name,
                    "type": "RISK_STOP_LOSS",
                    "title": "🚨 帶量跌破季線停損警報",
                    "desc": f"帶量跌破季線 MA60 ({ma60:.2f})！目前價: {price:.2f} 元 (放量 {vol_multiple:.2f}x)",
                    "price": price,
                    "ma_ref": ma60,
                    "vol_multiple": round(vol_multiple, 2),
                    "est_vol": int(est_vol)
                }

        # --- 【過熱濾網】：防追高機制 ---
        if bias_ma20 > cfg.MAX_BIAS_MA20:
            return None
        
        if rsi_14 > cfg.MAX_RSI_14:
            return None

        # 【指標3：爆天量防追高濾網】防止主力高檔巨量倒貨 (例如估量超過 3.5 倍均量)
        max_vol_burst = getattr(cfg, "MAX_VOLUME_BURST_MULTIPLE", 3.5)
        if vol_multiple > max_vol_burst:
            return None

        # 【指標4：均量黃金交叉確認】5日均量需大於20日均量 (MV5 > MV20) 表明量能擴增
        if getattr(cfg, "REQUIRE_VOL_GOLDEN_CROSS", True) and ma5_vol > 0 and ma20_vol > 0:
            if ma5_vol < ma20_vol:
                return None # 均量呈空頭排列，量能退潮不進場

        # 趨勢保護：MA20 不可大幅落後 MA60 (避免空頭重壓格局)
        if ma20 > 0 and ma60 > 0 and ma20 < ma60 * 0.97:
            return None

        # --- 【法人籌碼濾網】：防法人大舉倒貨出貨機制 ---
        inst = daily_ind.get("institutional", {})
        if inst.get("is_dumping", False):
            # 外資或投信近 3 日合計大賣出貨，即使盤中突破亦判定為主力誘多，不予追價
            return None

        inst_tag = inst.get("tag", "")
        inst_suffix = f" ({inst_tag})" if inst_tag and "常態" not in inst_tag else ""

        # --- 【策略一】：創高突破策略 ---
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
                "stock_id": stock_id,
                "stock_name": stock_name,
                "type": "STRATEGY_BREAKOUT",
                "title": f"🚀 創高突破買進訊號{inst_suffix}",
                "desc": f"突破 20 日高點 ({high_20:.2f})！推算全日量 {int(est_vol)} 張 (放量 {vol_multiple:.2f}x)",
                "price": price,
                "bias": round(bias_ma20, 2),
                "rsi": round(rsi_14, 2),
                "vol_multiple": round(vol_multiple, 2),
                "est_vol": int(est_vol),
                "high_20": high_20,
                "ma20": ma20,
                "institutional": inst,
                "entry_plan": {
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "max_chase": max_chase,
                    "stop_loss": sl_price,
                    "stop_loss_pct": stop_loss_pct,
                    "tp1": tp1_price,
                    "tp1_pct": tp1_pct,
                    "tp2": tp2_price,
                    "tp2_pct": tp2_pct
                }
            }

        # --- 【策略二】：月線起漲策略 ---
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
                "stock_id": stock_id,
                "stock_name": stock_name,
                "type": "STRATEGY_MA20_REBOUND",
                "title": f"📈 月線起漲買進訊號{inst_suffix}",
                "desc": f"剛自月線起漲 (MA20: {ma20:.2f})，位階安全，推算放量 {vol_multiple:.2f}x",
                "price": price,
                "bias": round(bias_ma20, 2),
                "rsi": round(rsi_14, 2),
                "vol_multiple": round(vol_multiple, 2),
                "est_vol": int(est_vol),
                "high_20": high_20,
                "ma20": ma20,
                "institutional": inst,
                "entry_plan": {
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "max_chase": max_chase,
                    "stop_loss": sl_price,
                    "stop_loss_pct": stop_loss_pct,
                    "tp1": tp1_price,
                    "tp1_pct": tp1_pct,
                    "tp2": tp2_price,
                    "tp2_pct": tp2_pct
                }
            }

        # --- 【策略三】：🏛️ 法人暗中布局起漲策略 ---
        # 當法人近 3 日積極默默買超，股價在月線附近安全打底 (-0.5% ~ +3.0%)，且盤中放量 >= 1.2x
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
                "stock_id": stock_id,
                "stock_name": stock_name,
                "type": "STRATEGY_INST_ACCUMULATION",
                "title": f"🏛️ 法人暗中布局起漲訊號 ({inst_tag})",
                "desc": f"三大法人近3日默默布局 (外資 {f_3d:+.0f}張 / 投信 {t_3d:+.0f}張)！股價自月線帶量發動起漲！",
                "price": price,
                "bias": round(bias_ma20, 2),
                "rsi": round(rsi_14, 2),
                "vol_multiple": round(vol_multiple, 2),
                "est_vol": int(est_vol),
                "high_20": high_20,
                "ma20": ma20,
                "institutional": inst,
                "entry_plan": {
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "max_chase": max_chase,
                    "stop_loss": sl_price,
                    "stop_loss_pct": stop_loss_pct,
                    "tp1": tp1_price,
                    "tp1_pct": tp1_pct,
                    "tp2": tp2_price,
                    "tp2_pct": tp2_pct
                }
            }

        return None
