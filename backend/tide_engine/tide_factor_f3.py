#!/usr/bin/env python3
"""
tide_factor_f3.py - 波动压缩
ATR(5) / ATR(20)，<0.6预示变盘
范围[0, +5]
"""
import os, sys, math
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def _calc_atr(rows, period):
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
    波动压缩因子
    ATR(5)/ATR(20) 比值越低 -> 压缩越紧 -> 越预示变盘 -> 分越高
    0.3以下=5分, 0.9以上=0分
    """
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
        atr5 = _calc_atr(rows[:6], 5)   # 最新6行才能算5日ATR
        atr20 = _calc_atr(rows, 20)
        if atr5 is None or atr20 is None or atr20 == 0:
            return 0.0
        ratio = atr5 / atr20
        # ratio 越低分越高
        # 0.3 -> 5分, 0.6 -> 3分, 1.0 -> 0分
        score = max(0.0, min(5.0, (1.0 - ratio) * 6.5))
        return round(score, 2)
    except:
        return 0.0
