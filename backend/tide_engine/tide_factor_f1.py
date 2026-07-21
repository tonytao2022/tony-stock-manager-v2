#!/usr/bin/env python3
"""
tide_factor_f1.py - 动量加速度
(20日涨幅 - 5日涨幅)/5，归一化到[-5, +5]
"""
import os, sys, math
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def compute(ts_code: str, trade_date: str) -> float:
    """
    动量加速度因子
    正值 = 加速上涨，负值 = 减速/下跌加速
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT close FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 20
        """, (ts_code, trade_date))
        rows = [float(r['close']) for r in cur.fetchall()]
        cur.close(); conn.close()
        if len(rows) < 20:
            return 0.0
        ret_20 = (rows[0] - rows[-1]) / rows[-1]
        ret_5 = (rows[0] - rows[5]) / rows[5]
        accel = (ret_20 - ret_5) / 5
        # 归一化: 0.015 ~= 1分
        score = max(-5.0, min(5.0, accel / 0.015))
        return round(score, 2)
    except Exception as e:
        print(f"  ⚠️ tid(tide_factor_f1.py) factor error: {e}")
        return 0.0
