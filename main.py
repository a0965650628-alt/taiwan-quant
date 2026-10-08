"""
main.py
系統啟動入口：整合盤中多執行緒零延遲掃描與盤後自動結算回報
支援參數：
  python main.py          : 盤中自動化監控 (08:50 開盤前晨報, 09:00~13:30 盤中掃描, 14:30 盤後結算)
  python main.py --morning: 手動推播【台股開盤前晨報】(美股四大指標 + 昨晚台指期夜盤)
  python main.py --settle : 手動強制執行當日盤後結算作業並發送統計報表
  python main.py --once   : 執行單次併發掃描後退出 (適用於排程/測試)
  python main.py --force  : 忽略交易時段限制，強制進行盤中連續掃描測試
  python main.py --test-line : 測試 LINE 推播通知連線
"""
import sys
import time
import logging
from datetime import datetime, time as dtime

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from config import cfg
from data_manager import DataManager
from alert_manager import AlertManager
from scanner import ConcurrencyScanner

import os
from logging.handlers import RotatingFileHandler

os.makedirs("logs", exist_ok=True)
log_format = '%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s'
handlers = [
    logging.StreamHandler(sys.stdout),
    RotatingFileHandler(os.path.join("logs", "quant_system.log"), maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
]

logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=handlers
)
logger = logging.getLogger("Main")

def is_market_open_time() -> bool:
    """判斷當前是否為台股正常撮合交易時段 (週一至週五 09:00 ~ 13:30)"""
    now = datetime.now()
    if now.weekday() >= 5:  # 週六與週日
        return False
    current_time = now.time()
    return dtime(9, 0) <= current_time <= dtime(13, 30)

def run_pre_market_briefing(alert_mgr: AlertManager):
    """
    開盤前晨報作業 (美股四大指標、TSM/NVDA 與昨晚台指期夜盤)
    """
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

def run_post_market_settlement(alert_mgr: AlertManager, data_mgr: DataManager):
    """
    盤後自動計算當日結算數據 (預設 14:30 執行)
    整合三大法人交易金額 (外資、投信、自營商)、個股籌碼動向與策略勝率
    """
    from institutional_manager import InstitutionalManager
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"========== 開始執行 {today} 14:30 盤後策略結算與法人籌碼作業 ==========")
    
    # 1. 抓取大盤三大法人交易金額
    inst_market = InstitutionalManager.fetch_market_institutional_summary()
    
    alerts = alert_mgr.get_today_alerts()
    target_sids = [str(item["stock_id"]) for item in alerts]
    
    # 若今日無觸發，亦拉取核心標的 (如台積電、聯發科、長榮、鴻海) 做為籌碼速報
    if not target_sids:
        target_sids = ["2330", "2317", "2454", "2603", "3037"]

    # 2. 抓取個股三大法人買賣超 (張數)
    stock_flows = InstitutionalManager.fetch_stocks_institutional_flow(target_sids)

    # 建立法人金額段落
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
        report_lines = [
            f"\n📊【{today} 14:30 盤後法人籌碼與策略日報】\n"
        ]
        report_lines.extend(inst_section_lines)
        report_lines.append("📋【今日策略狀態】\n今日無任何突破或停損訊號觸發，全數維持常態整理。")
        
        # 附上核心標的法人動向
        if stock_flows:
            report_lines.append("\n🔍【核心權值法人動向】")
            import twstock
            for sid in ["2330", "2317", "2454", "2603", "3037"]:
                flow = stock_flows.get(sid)
                if flow:
                    info = twstock.codes.get(sid)
                    sname = f" {info.name}" if info else ""
                    report_lines.append(
                        f"• {sid}{sname}：外資 {flow['foreign_lots']:+.0f}張 | 投信 {flow['trust_lots']:+.0f}張"
                    )

        final_report = "\n".join(report_lines)
        logger.info(final_report)
        alert_mgr.dispatch_line_message(final_report)
        logger.info("========== 盤後結算作業圓滿完成 ==========")
        return

    logger.info(f"今日共觸發 {len(alerts)} 筆警報，開始比對最新收盤績效與法人籌碼...")

    buy_pnl_list = []
    buy_wins = 0
    risk_count = 0
    detail_lines = []

    zh_type_map = {
        "RISK_STOP_LOSS": "🚨 停損風控",
        "STRATEGY_BREAKOUT": "🚀 創高突破",
        "STRATEGY_MA20_REBOUND": "📈 月線起漲",
        "STRATEGY_INST_ACCUMULATION": "🏛️ 法人暗中布局"
    }

    import twstock
    for idx, item in enumerate(alerts, 1):
        sid = item["stock_id"]
        signal_price = item["price"]
        atype = item["alert_type"]
        
        info = twstock.codes.get(sid)
        sname = f" {info.name}" if info else ""
        zh_type = zh_type_map.get(atype, atype)

        # 獲取最新收盤報價
        cached = data_mgr.get_cached_indicators(sid)
        if cached:
            close_price = cached.get("last_close", signal_price)
        else:
            rt = data_mgr.fetch_realtime_quote(sid)
            close_price = rt.get("current_price", signal_price) if rt else signal_price

        pnl_pct = ((close_price - signal_price) / signal_price) * 100.0 if signal_price > 0 else 0.0
        sign = "+" if pnl_pct >= 0 else ""
        
        flow = stock_flows.get(sid)
        flow_str = ""
        if flow:
            flow_str = f"\n   籌碼: 外資 {flow['foreign_lots']:+.0f}張 | 投信 {flow['trust_lots']:+.0f}張"

        if atype == "RISK_STOP_LOSS":
            risk_count += 1
            if pnl_pct < 0:
                status_desc = f"(避險少賠 {abs(pnl_pct):.2f}% 🛡️ 風控守紀律)"
            else:
                status_desc = f"(洗盤回升 +{pnl_pct:.2f}% ⚠️ 防守位震盪)"
            detail_lines.append(
                f"{idx}. {sid}{sname} [{zh_type}]\n"
                f"   觸發防守: {signal_price:.2f} ➜ 收盤: {close_price:.2f} {status_desc}{flow_str}"
            )
        else:
            buy_pnl_list.append(pnl_pct)
            if pnl_pct >= 0:
                buy_wins += 1
            status_desc = "🎯 獲利達標" if pnl_pct > 0 else "持平整理"
            detail_lines.append(
                f"{idx}. {sid}{sname} [{zh_type}]\n"
                f"   建議進場: {signal_price:.2f} ➜ 結算: {close_price:.2f} ({sign}{pnl_pct:.2f}% {status_desc}){flow_str}"
            )

    # 綜合績效統計
    total_alerts = len(alerts)
    perf_lines = [
        "🏆【今日策略運作績效】",
        f"• 觸發標的總數：{total_alerts} 檔"
    ]
    if buy_pnl_list:
        total_buys = len(buy_pnl_list)
        win_rate = (buy_wins / total_buys * 100.0)
        avg_pnl = sum(buy_pnl_list) / total_buys
        max_pnl = max(buy_pnl_list)
        min_pnl = min(buy_pnl_list)
        perf_lines.extend([
            f"• 🎯 買進策略勝率：{win_rate:.1f}% ({buy_wins}/{total_buys})",
            f"• 📈 買進平均損益：{avg_pnl:+.2f}%",
            f"• 🥇 最高買進表現：{max_pnl:+.2f}%",
            f"• 📉 最低買進表現：{min_pnl:+.2f}%"
        ])
    if risk_count > 0:
        perf_lines.append(f"• 🚨 觸發停損警戒：{risk_count} 檔 (嚴控風險，少虧為盈)")
    
    perf_lines.append("\n────────────────────\n")
    perf_lines.append("📋【各檔標的結算與籌碼明細】")

    # 手機友善排版 (法人數據與績效前置、卡片分段)
    report_lines = [
        f"\n📊【{today} 14:30 盤後策略與法人結算日報】\n"
    ]
    report_lines.extend(inst_section_lines)
    report_lines.extend(perf_lines)
    report_lines.extend(detail_lines)

    final_report = "\n".join(report_lines)
    logger.info(final_report)
    
    # 派送結算日報至 LINE
    alert_mgr.dispatch_line_message(final_report)
    logger.info("========== 盤後結算作業圓滿完成 ==========")

def main():
    args = sys.argv[1:]
    force_mode = "--force" in args
    once_mode = "--once" in args
    settle_mode = "--settle" in args
    morning_mode = "--morning" in args or "--briefing" in args
    session_mode = "--session" in args or "--intraday" in args
    test_line_mode = "--test-line" in args

    logger.info("高併發台股量化監控系統啟動...")
    data_mgr = DataManager()
    alert_mgr = AlertManager()
    scanner = ConcurrencyScanner(data_mgr, alert_mgr)

    # 測試 LINE 推播功能
    if test_line_mode:
        logger.info("執行 LINE 推播連線測試...")
        success = alert_mgr.dispatch_line_message(
            "🔔【台股量化通報】連線測試成功！\n"
            "這是一則系統測試訊息，代表您的 LINE Bot 推播功能已正確連接！"
        )
        if success:
            logger.info("✅ LINE 測試訊息已成功送達！")
        else:
            logger.error("❌ LINE 測試訊息發送失敗，請確認 Token 設定。")
        return

    # 開盤前晨報專屬模式
    if morning_mode:
        run_pre_market_briefing(alert_mgr)
        return

    # 1. 動態從 Google Sheets 同步 Watchlist
    watchlist = data_mgr.fetch_watchlist_from_google_sheets()
    logger.info(f"目前監控清單標的數量: {len(watchlist)} 檔")

    # 2. 預載日K技術指標至線程安全快取
    data_mgr.refresh_daily_indicators(watchlist)

    # 3. 處理特定模式
    if settle_mode:
        run_post_market_settlement(alert_mgr, data_mgr)
        return

    if once_mode:
        logger.info("執行單次平行掃描模式...")
        scanner.run_scan_cycle(watchlist)
        logger.info("單次掃描完成，程序退出。")
        return

    # 4. 常駐監控迴圈
    logger.info("進入自動化監控調度迴圈 (按下 Ctrl+C 即可安全退出)...")
    last_date = datetime.now().date()
    settlement_done_today = False
    morning_done_today = False

    # 若系統在開盤前時段啟動 (08:30 ~ 09:05 排程開機)，立即發送開盤前晨報
    now_startup = datetime.now()
    if dtime(8, 0) <= now_startup.time() <= dtime(9, 5) and now_startup.weekday() < 5:
        logger.info("檢測到為開盤前時段啟動，自動發送【台股開盤前晨報】...")
        run_pre_market_briefing(alert_mgr)
        morning_done_today = True

    try:
        while True:
            try:
                now = datetime.now()
                
                # 日期跨日重置
                if now.date() != last_date:
                    last_date = now.date()
                    settlement_done_today = False
                    morning_done_today = False
                    logger.info(f"跨入新交易日 {last_date}，重置當日任務標記。")

                # 盤中交易時段：執行高併發掃描
                if force_mode or is_market_open_time():
                    scanner.run_scan_cycle(watchlist)
                    time.sleep(cfg.SCAN_INTERVAL_SECONDS)
                    settlement_done_today = False  # 盤中重置結算標記
                else:
                    if session_mode and now.time() > dtime(13, 30):
                        logger.info("13:30 撮合收盤時間已到，盤中即時監控圓滿完成，程序退出。")
                        break

                    # 08:45 ~ 09:00 開盤前自動晨報 (若常駐守護進程未重啟)
                    if dtime(8, 45) <= now.time() < dtime(9, 0) and not morning_done_today and now.weekday() < 5:
                        logger.info("偵測到 08:50 開盤前時段，自動推播【台股開盤前晨報】...")
                        run_pre_market_briefing(alert_mgr)
                        morning_done_today = True

                    # 盤後自動結算時間 (14:30 ~ 14:45 區間且當日未結算，等待證交所公告三大法人籌碼)
                    elif dtime(14, 30) <= now.time() <= dtime(14, 45) and not settlement_done_today and now.weekday() < 5:
                        logger.info("偵測到 14:30 盤後結算時段，自動抓取三大法人交易金額與策略結算...")
                        run_post_market_settlement(alert_mgr, data_mgr)
                        settlement_done_today = True
                        time.sleep(300) # 休眠5分鐘避免重複觸發
                    else:
                        logger.info("非撮合時段，系統休眠中 (每 30 秒確認時鐘)...")
                        time.sleep(30)
            except Exception as loop_err:
                logger.critical(f"監控迴圈攔截到未預期異常 (自動復原重試中): {loop_err}", exc_info=True)
                time.sleep(5)

    except KeyboardInterrupt:
        logger.info("收到中斷訊號，系統安全關閉。")

if __name__ == "__main__":
    main()
