#!/usr/bin/env python3
"""
tide_factor_f5.py - 量价协整
price%与volume%的5日相关系数
同向=正分(量价配合)，反向=负分(量价背离)
归一化到[-5, +5]
"""
import os, sys, math
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def compute(ts_code: str, trade_date: str) -> float:
    """量价协整因子"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT change_pct, vol FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 6
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 6:
            return 0.0
        # 取最新5日
        pct_changes = [float(r['change_pct']) for r in rows[:5]]  # 收益%
        vols = [float(r['vol']) for r in rows[:5]]
        # vol%变化
        vol_changes = []
        for i in range(4):
            if vols[i+1] == 0: vol_changes.append(0.0)
            else: vol_changes.append((vols[i] - vols[i+1]) / vols[i+1] * 100)
        if len(vol_changes) < 4: return 0.0
        vol_changes.insert(0, 0.0)  # 对齐
        # 计算相关系数
        n = 5
        mean_p = sum(pct_changes) / n
        mean_v = sum(vol_changes) / n
        cov = sum((pct_changes[i] - mean_p) * (vol_changes[i] - mean_v) for i in range(n))
        var_p = sum((x - mean_p) ** 2 for x in pct_changes)
        var_v = sum((x - mean_v) ** 2 for x in vol_changes)
        if var_p == 0 or var_v == 0:
            return 0.0
        r = cov / (math.sqrt(var_p) * math.sqrt(var_v))
        score = max(-5.0, min(5.0, r * 5.0))
        return round(score, 2)
    except:
        return 0.0
