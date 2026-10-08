"""
pre_market_manager.py
開盤前國際總經與期貨晨報模組
支援：
  1. 美股四大指數 (道瓊、標普500、那斯達克、費城半導體)
  2. 台積電 ADR (TSM) 與 輝達 (NVDA)
  3. 台灣期貨交易所 (TAIFEX) 昨晚台指期夜盤收盤、漲跌與成交量
"""
import logging
import requests
from typing import Dict, Any, Optional
from bs4 import BeautifulSoup
import yfinance as yf

logger = logging.getLogger("PreMarketManager")

class PreMarketManager:
    TAIFEX_REPORT_URL = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    @classmethod
    def fetch_us_indices(cls) -> Dict[str, Dict[str, Any]]:
        """
        獲取美股四大指數及關鍵權值 ADR (道瓊、S&P500、那斯達克、費半、台積電ADR、輝達)
        """
        indices = {
            "^DJI": "道瓊工業 (DJI)",
            "^GSPC": "標普 500 (S&P 500)",
            "^IXIC": "那斯達克 (Nasdaq)",
            "^SOX": "費城半導體 (SOX)",
            "TSM": "台積電 ADR (TSM)",
            "NVDA": "輝達 (NVDA)"
        }
        result = {}
        for sym, name in indices.items():
            try:
                t = yf.Ticker(sym)
                fi = t.fast_info
                p = getattr(fi, "last_price", None)
                pc = getattr(fi, "previous_close", None)
                if p is not None and pc is not None and pc > 0:
                    chg = p - pc
                    pct = (chg / pc) * 100.0
                    result[sym] = {
                        "name": name,
                        "price": round(float(p), 2),
                        "change": round(float(chg), 2),
                        "pct": round(float(pct), 2)
                    }
                else:
                    # 備援歷史日K最新一天
                    df = t.history(period="5d", timeout=5)
                    if not df.empty and len(df) >= 2:
                        p = df['Close'].iloc[-1]
                        pc = df['Close'].iloc[-2]
                        chg = p - pc
                        pct = (chg / pc) * 100.0
                        result[sym] = {
                            "name": name,
                            "price": round(float(p), 2),
                            "change": round(float(chg), 2),
                            "pct": round(float(pct), 2)
                        }
            except Exception as e:
                logger.warning(f"獲取美股指標 [{sym}] 異常: {e}")

        return result

    @classmethod
    def fetch_taiwan_night_futures(cls) -> Optional[Dict[str, Any]]:
        """
        從期交所 (TAIFEX) 獲取昨晚台指期夜盤 (TX 近月) 最新行情
        """
        try:
            payload = {
                "queryType": "2",     # 盤後交易時段 (夜盤)
                "marketCode": "1",    # 盤後
                "commodity_id": "TX"  # 台指期
            }
            res = requests.post(cls.TAIFEX_REPORT_URL, data=payload, headers=cls.HEADERS, timeout=10)
            if res.status_code != 200:
                logger.warning(f"TAIFEX 請求回應失敗: {res.status_code}")
                return None

            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table", {"class": "table_f"})
            if not table:
                logger.warning("未於期交所頁面找到夜盤行情表格")
                return None

            rows = table.find_all("tr")
            if len(rows) < 2:
                return None

            # 第一筆資料通常為近月合約
            cols = [td.text.strip().replace("\n", "").replace("\r", "") for td in rows[1].find_all(["td", "th"])]
            if len(cols) >= 8:
                contract = cols[1]       # 到期月份
                open_p = cols[2]         # 開盤
                high_p = cols[3]         # 最高
                low_p = cols[4]          # 最低
                close_p = cols[5]        # 最新/收盤
                diff_raw = cols[6].strip()
                is_down = "▼" in diff_raw or "-" in diff_raw
                num_part = diff_raw.replace("▲", "").replace("▼", "").replace("+", "").replace("-", "").strip()
                diff = f"-{num_part}" if is_down else f"+{num_part}"

                pct_raw = cols[7].strip()
                pct_is_down = "▼" in pct_raw or "-" in pct_raw
                pct_num = pct_raw.replace("▲", "").replace("▼", "").replace("+", "").replace("-", "").replace("%", "").strip()
                diff_pct = f"-{pct_num}%" if pct_is_down else f"+{pct_num}%"

                vol = cols[8] if len(cols) > 8 else "0" # 成交量

                return {
                    "contract": contract,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "diff": diff,
                    "diff_pct": diff_pct,
                    "volume": vol
                }
        except Exception as e:
            logger.error(f"獲取台指期夜盤行情異常: {e}")
            return None

    @classmethod
    def generate_pre_market_report(cls) -> str:
        """
        整合美股四大指標與台指期夜盤，產出手機友善的 LINE 開盤晨報
        """
        from datetime import datetime
        today_str = datetime.now().strftime("%Y-%m-%d")
        now_time = datetime.now().strftime("%H:%M")

        us_data = cls.fetch_us_indices()
        fut_data = cls.fetch_taiwan_night_futures()

        report_lines = [
            f"\n🌅【台股開盤前晨報 - 國際總經與夜盤速報】\n",
            f"📅 統計時間：{today_str} {now_time}\n",
            "🇺🇸【美股四大指數與關鍵權值】"
        ]

        # 整理四大指數
        major_syms = ["^DJI", "^GSPC", "^IXIC", "^SOX"]
        for sym in major_syms:
            if sym in us_data:
                item = us_data[sym]
                sign = "+" if item["change"] >= 0 else ""
                report_lines.append(
                    f"• {item['name']}：{item['price']:,.2f} ({sign}{item['change']:.2f}, {sign}{item['pct']:.2f}%)"
                )

        # 整理關鍵權值 ADR (TSM, NVDA)
        adr_lines = []
        for sym in ["TSM", "NVDA"]:
            if sym in us_data:
                item = us_data[sym]
                sign = "+" if item["change"] >= 0 else ""
                adr_lines.append(
                    f"• {item['name']}：{item['price']:.2f} ({sign}{item['change']:.2f}, {sign}{item['pct']:.2f}%)"
                )
        if adr_lines:
            report_lines.append("────────────────────")
            report_lines.extend(adr_lines)

        report_lines.append("\n────────────────────\n")
        report_lines.append("🌙【昨晚台指期夜盤 (TX 近月)】")

        if fut_data:
            diff_val = fut_data["diff"].strip()
            if not diff_val.startswith(("+", "-")):
                diff_val = f"+{diff_val}"
            diff_pct_val = fut_data["diff_pct"].strip()
            if not diff_pct_val.startswith(("+", "-")):
                diff_pct_val = f"+{diff_pct_val}"

            close_fmt = f"{int(fut_data['close']):,}" if fut_data['close'].isdigit() else fut_data['close']
            vol_fmt = f"{int(fut_data['volume']):,}" if fut_data['volume'].isdigit() else fut_data['volume']

            report_lines.extend([
                f"• 最新收盤價：{close_fmt} 點",
                f"• 漲跌點數：{diff_val} 點 ({diff_pct_val})",
                f"• 夜盤震盪區間：{fut_data['low']} ~ {fut_data['high']} 點",
                f"• 總成交口數：{vol_fmt} 口"
            ])
        else:
            report_lines.append("• 昨晚台指期夜盤資料讀取中或休市")

        report_lines.append("\n────────────────────\n")
        report_lines.append(
            "💡【開盤量化策略提醒】\n"
            "• 09:00 開盤起系統自動進行 59 檔股票高併發即時監控！\n"
            "• 鎖定：帶量突破、月線起漲與三大法人暗中布局股。"
        )

        return "\n".join(report_lines)
