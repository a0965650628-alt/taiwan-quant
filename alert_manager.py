"""
alert_manager.py
警報管理中心：執行緒安全本地持久化 (SQLite)、當日防重複發送、LINE Notify & LINE Bot 即時推播
"""
import sqlite3
import logging
import threading
import requests
from datetime import datetime
from typing import Dict, Any, List
from config import cfg

logger = logging.getLogger("AlertManager")

class AlertManager:
    def __init__(self, db_path: str = cfg.DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._sent_today = set() # 存放 (stock_id, alert_type) 記憶體快速去重
        self._init_db()

    def _init_db(self):
        """初始化 SQLite 警報記錄庫並載入今日已發送記錄"""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("""
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
                
                # 載入當日已發送名單，防止程式重啟後重複洗頻
                today = datetime.now().strftime("%Y-%m-%d")
                cursor.execute("SELECT stock_id, alert_type FROM alert_logs WHERE date = ?", (today,))
                for sid, atype in cursor.fetchall():
                    self._sent_today.add((str(sid), str(atype)))
                conn.close()
                logger.info(f"警報資料庫初始化完成，今日已記錄 {len(self._sent_today)} 則警報")
            except Exception as e:
                logger.error(f"SQLite 初始化失敗: {e}")

    def send_alert(self, signal: Dict[str, Any]) -> bool:
        """
        發送警報 (執行緒安全，同標的同訊號當日僅發送一次)
        :return: True 代表成功發送，False 代表重複或被攔截
        """
        sid = str(signal["stock_id"])
        atype = str(signal["type"])
        key = (sid, atype)

        with self._lock:
            if key in self._sent_today:
                return False  # 當日同商品同策略已推播過，予以攔截
            self._sent_today.add(key)

        # 取得公司繁體中文名稱
        stock_name = signal.get("stock_name", "")
        if not stock_name:
            import twstock
            info = twstock.codes.get(sid)
            stock_name = info.name if info else ""
        name_str = f" {stock_name}" if stock_name else ""

        now_str = datetime.now().strftime("%H:%M:%S")
        price = float(signal.get('price', 0.0))
        est_vol = int(signal.get('est_vol', 0))
        vol_multiple = float(signal.get('vol_multiple', 1.0))
        plan = signal.get("entry_plan")

        if plan:
            # 買進訊號專用：學生保本交易計畫排版 (清晰卡片分段，手機閱讀寬鬆舒適)
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
            # 停損或一般警報排版
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

        # 1. 寫入本地 SQLite
        self._persist_to_db(signal, msg)

        # 2. 外部推播 (LINE Notify / LINE Bot)
        self.dispatch_line_message(msg)
        return True

    def _persist_to_db(self, signal: Dict[str, Any], msg: str):
        """保存警報至 SQLite"""
        now = datetime.now()
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO alert_logs (date, timestamp, stock_id, alert_type, price, vol_multiple, message)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    now.strftime("%Y-%m-%d"),
                    now.strftime("%H:%M:%S"),
                    str(signal["stock_id"]),
                    str(signal["type"]),
                    float(signal.get("price", 0.0)),
                    float(signal.get("vol_multiple", 0.0)),
                    msg
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"寫入 SQLite 警報記錄失敗: {e}")

    def dispatch_line_message(self, message: str) -> bool:
        """
        發送 LINE 訊息：優先支援 LINE Notify，亦相容 LINE Messaging API (Push Message)
        網路請求嚴格包含 Timeout 與例外捕獲
        """
        # 1. 優先檢查 LINE Notify
        if cfg.LINE_NOTIFY_TOKEN and not cfg.LINE_NOTIFY_TOKEN.startswith("YOUR_"):
            try:
                url = "https://notify-api.line.me/api/notify"
                headers = {"Authorization": f"Bearer {cfg.LINE_NOTIFY_TOKEN}"}
                payload = {"message": message}
                res = requests.post(url, headers=headers, data=payload, timeout=cfg.HTTP_TIMEOUT)
                if res.status_code == 200:
                    logger.info("LINE Notify 推播發送成功")
                    return True
                else:
                    logger.warning(f"LINE Notify 回應異常: {res.status_code} - {res.text}")
            except Exception as e:
                logger.error(f"LINE Notify 網路請求異常: {e}")

        # 2. 檢查 LINE Messaging API Bot (支援特定 User 推播 或 全員廣播)
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
                    # 未設定 User ID 時自動採用 Broadcast 廣播給所有加好友的使用者
                    url = "https://api.line.me/v2/bot/message/broadcast"
                    body = {
                        "messages": [{"type": "text", "text": message.strip()}]
                    }
                res = requests.post(url, headers=headers, json=body, timeout=cfg.HTTP_TIMEOUT)
                if res.status_code == 200:
                    logger.info("LINE Messaging API Bot 推播發送成功")
                    return True
                else:
                    logger.warning(f"LINE Bot 回應異常: {res.status_code} - {res.text}")
            except Exception as e:
                logger.error(f"LINE Bot 網路請求異常: {e}")

        # 3. 未設定 Token 時的本地模擬終端輸出
        logger.info(f"📢 [本地終端即時警報模擬]{message}")
        return True

    def get_today_alerts(self) -> List[Dict[str, Any]]:
        """讀取今日所有警報記錄 (供盤後結算使用)"""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT stock_id, alert_type, price, vol_multiple, timestamp 
                    FROM alert_logs 
                    WHERE date = ? 
                    ORDER BY id ASC
                """, (today,))
                rows = cursor.fetchall()
                conn.close()
                return [
                    {
                        "stock_id": r[0],
                        "alert_type": r[1],
                        "price": r[2],
                        "vol_multiple": r[3],
                        "timestamp": r[4]
                    }
                    for r in rows
                ]
            except Exception as e:
                logger.error(f"讀取今日警報記錄失敗: {e}")
                return []
