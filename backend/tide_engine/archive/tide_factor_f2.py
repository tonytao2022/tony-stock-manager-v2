#!/usr/bin/env python3
"""
tide_factor_f2.py - 突破强度
(当日最高 - 20日均线) / ATR(14) 
归一化到[-5, +5]
"""
import os, sys, math
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def _calc_atr(rows, period=14):
    """计算ATR"""
    if len(rows) < period + 1:
        return None
    tr_sum = 0.0
    for i in range(1, min(period + 1, len(rows))):
        h, l, pc = float(rows[i]['high']), float(rows[i]['low']), float(rows[i-1]['close'])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_sum += tr
    return tr_sum / min(period, len(rows) - 1)


def compute(ts_code: str, trade_date: str) -> float:
    """突破强度因子"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, high, low, close FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 21
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 21:
            return 0.0
        # 当日数据
        today = rows[0]
        today_high = float(today['high'])
        today_close = float(today['close'])
        # 20日均线
        ma20 = sum(float(r['close']) for r in rows[:20]) / 20
        # ATR
        atr = _calc_atr(rows)
        if atr is None or atr == 0:
            return 0.0
        raw = (today_high - ma20) / atr
        # 归一化: raw≈2.0 => 5分
        score = max(-5.0, min(5.0, raw / 0.4))
        return round(score, 2)
    except:
        return 0.0
