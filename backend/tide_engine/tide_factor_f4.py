#!/usr/bin/env python3
"""
tide_factor_f4.py - 回摆势能因子（合并原F2+F4+F7）
测量价格偏离均线的程度，方向无关，绝对值越大势能越大
计算: (|close - ma20| + |close - ma30|) / (ATR14 * 2)
归一化到[0, +5]
"""
import os, sys, math
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def _calc_atr(rows, period=14):
    if len(rows) < period + 1:
        return None
    tr_sum = 0.0
    for i in range(1, min(period + 1, len(rows))):
        h, l, pc = float(rows[i]['high']), float(rows[i]['low']), float(rows[i-1]['close'])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_sum += tr
    return tr_sum / min(period, len(rows) - 1)


def compute(ts_code: str, trade_date: str) -> float:
    """
    回摆势能因子
    A股反转效应最强，所以越高越好（偏离越远越可能回归）
    范围[0, +5]
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT close, high, low FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 30
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 21:
            return 0.0
        today_close = float(rows[0]['close'])
        closes = [float(r['close']) for r in rows]
        ma20 = sum(closes[:20]) / 20
        ma30 = sum(closes) / 30
        # ATR
        atr = _calc_atr(rows)
        if atr is None or atr == 0:
            return 0.0
        # 综合偏离度: (|close-ma20| + |close-ma30|) / (2 * ATR)
        raw = (abs(today_close - ma20) + abs(today_close - ma30)) / (2 * atr)
        # raw=1.5 → 3分, raw=3.0 → 5分
        score = min(5.0, raw / 0.6)
        return round(score, 2)
    except Exception as e:
        print(f"  ⚠️ tid(tide_factor_f4.py) factor error: {e}")
        return 0.0
