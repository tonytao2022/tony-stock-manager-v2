#!/usr/bin/env python3
"""
tide_factor_f7.py - 基底支撑
(收盘 - 30日均线) / 30日收盘标准差
正值=在均线上方(支撑)，负值=在均线下方(压力)
归一化到[-5, +5]
"""
import os, sys, math
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def compute(ts_code: str, trade_date: str) -> float:
    """基底支撑因子"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT close FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 30
        """, (ts_code, trade_date))
        rows = [float(r['close']) for r in cur.fetchall()]
        cur.close(); conn.close()
        if len(rows) < 30:
            return 0.0
        today_close = rows[0]
        ma30 = sum(rows) / 30
        # 标准差
        variance = sum((x - ma30)**2 for x in rows) / 30
        std = math.sqrt(variance)
        if std == 0:
            return 0.0
        z_score = (today_close - ma30) / std
        score = max(-5.0, min(5.0, z_score))
        return round(score, 2)
    except:
        return 0.0
