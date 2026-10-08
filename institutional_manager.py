"""
institutional_manager.py
台股三大法人交易金額與個股買賣超數據爬取模組
支援：證交所 (TWSE) 大盤法人買賣金額、上市個股三大法人、櫃買中心 (TPEx) 上櫃個股三大法人
"""
import logging
import requests
from typing import Dict, List, Any, Optional

logger = logging.getLogger("InstitutionalManager")

class InstitutionalManager:
    TWSE_MARKET_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"
    TWSE_STOCKS_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALLBUT0999"
    TPEX_STOCKS_URL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    TIMEOUT = 12

    @classmethod
    def fetch_market_institutional_summary(cls, max_retries: int = 3, retry_delay: int = 10) -> Optional[Dict[str, Any]]:
        """
        獲取今日大盤三大法人交易金額 (單位：億元)
        支援證交所 14:30 延遲公告自動輪詢重試 (預設重試 3 次)
        回傳: 外資、投信、自營商及合計買賣超金額
        """
        import time
        for attempt in range(1, max_retries + 1):
            try:
                res = requests.get(cls.TWSE_MARKET_URL, headers=cls.HEADERS, timeout=cls.TIMEOUT)
                res.raise_for_status()
                data = res.json()
                
                if data.get("stat") != "OK" or "data" not in data:
                    if attempt < max_retries:
                        logger.info(f"TWSE 三大法人金額尚未公佈 (第 {attempt} 次檢測)，等待 {retry_delay} 秒後重試...")
                        time.sleep(retry_delay)
                        continue
                    else:
                        logger.warning(f"TWSE 三大法人金額目前尚未公佈或無資料: {data.get('stat')}")
                        return None

                market_summary = {}
                for row in data.get("data", []):
                    name = row[0].replace(" ", "").strip()
                    diff_str = row[3].replace(",", "").strip()
                    try:
                        diff_val = int(diff_str)
                        market_summary[name] = diff_val
                    except ValueError:
                        continue

                foreign = market_summary.get("外資及陸資(不含外資自營商)", 0) / 1e8
                trust = market_summary.get("投信", 0) / 1e8
                dealer_self = market_summary.get("自營商(自行買賣)", 0) / 1e8
                dealer_hedge = market_summary.get("自營商(避險)", 0) / 1e8
                dealer = dealer_self + dealer_hedge
                total = market_summary.get("合計", 0) / 1e8

                return {
                    "title": data.get("title", "三大法人買賣金額統計"),
                    "date": data.get("date", ""),
                    "foreign_billion": foreign,
                    "trust_billion": trust,
                    "dealer_billion": dealer,
                    "total_billion": total
                }
            except Exception as e:
                logger.error(f"獲取大盤三大法人金額嘗試 {attempt}/{max_retries} 失敗: {e}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
        return None

    @classmethod
    def fetch_stocks_institutional_flow(cls, target_sids: Optional[List[str]] = None) -> Dict[str, Dict[str, float]]:
        """
        獲取個股三大法人買賣超 (張數)
        整合 TWSE 上市 + TPEx 上櫃
        回傳字典格式: { "2330": { "foreign_lots": -1163.3, "trust_lots": 72.8, "total_lots": -643.5 } }
        """
        result = {}
        target_set = set(target_sids) if target_sids else None

        # 1. 抓取 TWSE (上市股票)
        try:
            res = requests.get(cls.TWSE_STOCKS_URL, headers=cls.HEADERS, timeout=cls.TIMEOUT)
            if res.status_code == 200:
                d = res.json()
                for row in d.get("data", []):
                    sid = row[0].strip()
                    if target_set and sid not in target_set:
                        continue
                    try:
                        foreign_shares = int(row[4].replace(",", "").strip())
                        trust_shares = int(row[10].replace(",", "").strip())
                        total_shares = int(row[18].replace(",", "").strip()) if len(row) > 18 else int(row[-1].replace(",", "").strip())
                        result[sid] = {
                            "foreign_lots": round(foreign_shares / 1000.0, 1),
                            "trust_lots": round(trust_shares / 1000.0, 1),
                            "total_lots": round(total_shares / 1000.0, 1)
                        }
                    except (ValueError, IndexError):
                        continue
        except Exception as e:
            logger.warning(f"獲取 TWSE 個股法人動向失敗: {e}")

        # 2. 抓取 TPEx (上櫃股票)
        try:
            res = requests.get(cls.TPEX_STOCKS_URL, headers=cls.HEADERS, timeout=cls.TIMEOUT)
            if res.status_code == 200:
                d = res.json()
                tables = d.get("tables", [])
                t0_data = tables[0].get("data", []) if tables else []
                for row in t0_data:
                    sid = row[0].strip()
                    if target_set and sid not in target_set:
                        continue
                    try:
                        # 欄位順序: 外資買賣超(7), 投信買賣超(10), 三大法人合計買賣超(最後一欄)
                        foreign_shares = int(row[7].replace(",", "").strip())
                        trust_shares = int(row[10].replace(",", "").strip())
                        total_shares = int(row[-1].replace(",", "").strip())
                        result[sid] = {
                            "foreign_lots": round(foreign_shares / 1000.0, 1),
                            "trust_lots": round(trust_shares / 1000.0, 1),
                            "total_lots": round(total_shares / 1000.0, 1)
                        }
                    except (ValueError, IndexError):
                        continue
        except Exception as e:
            logger.warning(f"獲取 TPEx 個股法人動向失敗: {e}")

        return result

    @classmethod
    def get_recent_trading_dates(cls, n: int = 3) -> List[str]:
        """推算最近的有效交易日清單 (跳過週末)"""
        from datetime import datetime, timedelta
        dates = []
        curr = datetime.now()
        while len(dates) < n + 3:
            if curr.weekday() < 5:
                dates.append(curr.strftime("%Y%m%d"))
            curr -= timedelta(days=1)
        return dates

    @classmethod
    def fetch_daily_flow_for_date(cls, dt_str: str) -> Dict[str, Dict[str, float]]:
        """抓取指定日期的個股三大法人買賣超 (具本地快取防重抓)"""
        import os, json
        cache_dir = "./cache_kline"
        cache_path = os.path.join(cache_dir, f"inst_{dt_str}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        flow = {}
        # 1. TWSE 上市
        try:
            url = f"{cls.TWSE_STOCKS_URL}&date={dt_str}"
            res = requests.get(url, headers=cls.HEADERS, timeout=cls.TIMEOUT)
            if res.status_code == 200:
                d = res.json()
                for r in d.get("data", []):
                    try:
                        sid = r[0].strip()
                        flow[sid] = {
                            "foreign": int(r[4].replace(",", "")) / 1000.0,
                            "trust": int(r[10].replace(",", "")) / 1000.0,
                            "total": int(r[18].replace(",", "")) / 1000.0 if len(r) > 18 else 0.0
                        }
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"TWSE {dt_str} 法人拉取異常: {e}")

        # 2. TPEx 上櫃
        try:
            roc_year = int(dt_str[:4]) - 1911
            roc_date = f"{roc_year}/{dt_str[4:6]}/{dt_str[6:]}"
            tpex_url = f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D&d={roc_date}"
            res_tpex = requests.get(tpex_url, headers=cls.HEADERS, timeout=cls.TIMEOUT)
            if res_tpex.status_code == 200:
                d_tpex = res_tpex.json()
                tables = d_tpex.get("tables", [])
                if tables and tables[0].get("data"):
                    for r in tables[0]["data"]:
                        try:
                            sid = r[0].strip()
                            flow[sid] = {
                                "foreign": int(r[7].replace(",", "")) / 1000.0,
                                "trust": int(r[10].replace(",", "")) / 1000.0,
                                "total": int(r[-1].replace(",", "")) / 1000.0
                            }
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"TPEx {dt_str} 法人拉取異常: {e}")

        if flow:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(flow, f, ensure_ascii=False)
            except Exception:
                pass

        return flow

    @classmethod
    def fetch_recent_accumulation(cls, target_sids: Optional[List[str]] = None, days: int = 3) -> Dict[str, Dict[str, Any]]:
        """
        計算近 N 日三大法人累積籌碼與布局等級
        回傳: { "2330": { "foreign_3d": 6934.9, "trust_3d": 816.5, "total_3d": 9086.1, "is_accumulating": True, "tag": "🏛️ 土洋同步認養布局" } }
        """
        candidate_dates = cls.get_recent_trading_dates(days)
        valid_flows = []
        for dt_str in candidate_dates:
            daily_flow = cls.fetch_daily_flow_for_date(dt_str)
            if daily_flow:
                valid_flows.append(daily_flow)
            if len(valid_flows) >= days:
                break

        agg = {}
        for day_f in valid_flows:
            for sid, val in day_f.items():
                if target_sids and sid not in target_sids:
                    continue
                if sid not in agg:
                    agg[sid] = {"foreign_3d": 0.0, "trust_3d": 0.0, "total_3d": 0.0}
                agg[sid]["foreign_3d"] += val.get("foreign", 0.0)
                agg[sid]["trust_3d"] += val.get("trust", 0.0)
                agg[sid]["total_3d"] += val.get("total", 0.0)

        result = {}
        target_list = target_sids if target_sids else list(agg.keys())
        for sid in target_list:
            d = agg.get(sid, {"foreign_3d": 0.0, "trust_3d": 0.0, "total_3d": 0.0})
            f_3d = round(d["foreign_3d"], 1)
            t_3d = round(d["trust_3d"], 1)
            tot_3d = round(d["total_3d"], 1)

            # 判斷法人布局型態
            if t_3d > 50 and f_3d > 100:
                tag = "🏛️ 土洋同步聯手布局"
                accum = True
                dump = False
            elif t_3d > 30:
                tag = f"🏛️ 投信積極認養 (+{t_3d:+.0f}張)"
                accum = True
                dump = False
            elif f_3d > 200:
                tag = f"🏛️ 外資大舉敲進 (+{f_3d:+.0f}張)"
                accum = True
                dump = False
            elif tot_3d < -1000 or (f_3d < -500 and t_3d < 0):
                tag = f"⚠️ 法人連日倒貨提款 ({tot_3d:+.0f}張)"
                accum = False
                dump = True
            elif tot_3d > 50:
                tag = f"🏛️ 法人暗中吸籌 (+{tot_3d:+.0f}張)"
                accum = True
                dump = False
            else:
                tag = "常態中性籌碼"
                accum = False
                dump = False

            result[sid] = {
                "foreign_3d": f_3d,
                "trust_3d": t_3d,
                "total_3d": tot_3d,
                "is_accumulating": accum,
                "is_dumping": dump,
                "tag": tag
            }

        return result
