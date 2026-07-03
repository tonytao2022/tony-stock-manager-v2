#!/usr/bin/env python3
"""
tide_factor_f6.py - 趋势质量 (ADX+DI)
ADX(14) + DI+/DI- 方向判断
正值=强趋势多向，负值=强趋势空向，0=无趋势
归一化到[-5, +5]
"""
import os, sys, math
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def compute(ts_code: str, trade_date: str) -> float:
    """趋势质量因子（ADX+DI）"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT high, low, close FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date ASC LIMIT 15
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 15:
            return 0.0
        # 计算+DM, -DM, TR
        plus_dm_list, minus_dm_list, tr_list = [], [], []
        for i in range(1, len(rows)):
            h1, l1, c1 = float(rows[i-1]['high']), float(rows[i-1]['low']), float(rows[i-1]['close'])
            h2, l2 = float(rows[i]['high']), float(rows[i]['low'])
            up_move = h2 - h1
            down_move = l1 - l2
            tr = max(h2 - l2, abs(h2 - c1), abs(l2 - c1))
            tr_list.append(tr)
            if up_move > down_move and up_move > 0:
                plus_dm_list.append(up_move)
            else:
                plus_dm_list.append(0)
            if down_move > up_move and down_move > 0:
                minus_dm_list.append(down_move)
            else:
                minus_dm_list.append(0)
        if len(tr_list) < 14:
            return 0.0
        # 平滑
        atr = sum(tr_list) / 14
        sum_plus = sum(plus_dm_list)
        sum_minus = sum(minus_dm_list)
        if atr == 0:
            return 0.0
        di_plus = (sum_plus / 14) / atr * 100
        di_minus = (sum_minus / 14) / atr * 100
        dx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else 0
        adx = dx  # 近似：单周期DX≈ADX
        # 方向: +DI > -DI = 正趋势
        direction = 1 if di_plus > di_minus else -1
        raw = adx / 100 * 10 * direction
        score = max(-5.0, min(5.0, raw))
        return round(score, 2)
    except:
        return 0.0
