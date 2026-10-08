"""
volume_estimator.py
台股盤中 U 型微笑曲線動態全日成交量推算引擎
"""
from datetime import datetime, time
from typing import Tuple

class VolumeEstimator:
    """
    基於台股歷史經驗累積成交量比例 (U型曲線) 的內插推算模型
    台股交易時段：09:00 ~ 13:30，共 270 分鐘
    """
    # (分, 累積成交量百分比基準)
    CUMULATIVE_BENCHMARKS = [
        (0,   0.00),   # 09:00 開盤
        (5,   0.08),   # 09:05 
        (15,  0.18),   # 09:15 
        (30,  0.28),   # 09:30 開盤前半小時通常已釋放近三成量能
        (60,  0.40),   # 10:00 進入平緩期
        (90,  0.50),   # 10:30
        (120, 0.58),   # 11:00
        (150, 0.65),   # 11:30 中午低谷期
        (180, 0.72),   # 12:00
        (210, 0.80),   # 12:30
        (240, 0.88),   # 13:00 尾盤開始放量
        (265, 0.94),   # 13:25 最後試撮前
        (270, 1.00)    # 13:30 收盤
    ]

    @classmethod
    def get_market_elapsed_minutes(cls, now_dt: datetime) -> int:
        """計算當前時間距離 09:00 的開盤分鐘數 (0 ~ 270)"""
        market_open = now_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        market_close = now_dt.replace(hour=13, minute=30, second=0, microsecond=0)

        if now_dt < market_open:
            return 0
        if now_dt >= market_close:
            return 270
        
        diff = now_dt - market_open
        return int(diff.total_seconds() // 60)

    @classmethod
    def get_cumulative_ratio(cls, elapsed_mins: int) -> float:
        """使用線性內插法獲取當前分鐘的累積成交量比例"""
        if elapsed_mins <= 0:
            return 0.05  # 開盤前5分鐘防零除與過度放大保護
        if elapsed_mins >= 270:
            return 1.00

        for i in range(len(cls.CUMULATIVE_BENCHMARKS) - 1):
            t1, r1 = cls.CUMULATIVE_BENCHMARKS[i]
            t2, r2 = cls.CUMULATIVE_BENCHMARKS[i + 1]
            if t1 <= elapsed_mins <= t2:
                # 線性內插
                factor = (elapsed_mins - t1) / (t2 - t1)
                return r1 + factor * (r2 - r1)
        return 1.00

    @classmethod
    def estimate_full_day_volume(cls, current_volume: float, now_dt: datetime = None) -> Tuple[float, float]:
        """
        推算全日成交量
        :param current_volume: 目前累積成交量 (張或股)
        :param now_dt: 基準時間 (預設 datetime.now())
        :return: (預估全日量, 當前累積比例)
        """
        if now_dt is None:
            now_dt = datetime.now()
            
        elapsed_mins = cls.get_market_elapsed_minutes(now_dt)
        ratio = cls.get_cumulative_ratio(elapsed_mins)
        
        # 避免剛開盤前數分鐘數據極端放大，設定最低比例為 3%
        effective_ratio = max(ratio, 0.03)
        estimated_volume = current_volume / effective_ratio
        return round(estimated_volume, 2), round(effective_ratio, 4)
