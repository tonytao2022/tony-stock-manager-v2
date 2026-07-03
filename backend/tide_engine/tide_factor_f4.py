#!/usr/bin/env python3
"""
tide_factor_f4.py - 均值回复势能（最高权重因子）
(20日均线 - 当前价) / ATR(14)
正数 = 超卖回归向上，负数 = 超买回归向下
归一化到[-5, +5]
"""
import os, sys
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
    """均值回复势能因子"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT high, low, close FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 21
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 21:
            return 0.0
        today_close = float(rows[0]['close'])
        ma20 = sum(float(r['close']) for r in rows[:20]) / 20
        atr = _calc_atr(rows)
        if atr is None or atr == 0:
            return 0.0
        # 当前价在均线下方->正分(回归向上)，上方->负分(回归向下)
        raw = (ma20 - today_close) / atr
        # raw≈1.5 => 5分
        score = max(-5.0, min(5.0, raw / 0.3))
        return round(score, 2)
    except:
        return 0.0
