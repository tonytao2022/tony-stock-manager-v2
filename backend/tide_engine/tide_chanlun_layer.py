#!/usr/bin/env python3
"""
tide_chanlun_layer.py - 缠论信号层
不做独立评分，只做信号确认/抑制:
  1. 中枢突破 → 确认F2/F6
  2. 笔段背驰 → 抑制F1/F2
  3. 三买形态 → 辅助确认F4
"""
import os, sys, math
from typing import Dict, Optional

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection


def detect_central_breakthrough(ts_code: str, trade_date: str) -> bool:
    """
    中枢突破检测
    条件：近30日存在宽幅≤15%的密集区，当日close突破区间
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT high, low, close, vol FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 31
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 21:  # 至少需要21日才有意义
            return False
        today = rows[0]
        today_close = float(today['close'])
        today_vol = float(today['vol'])
        # 取30日（不含当日）的高低点
        highs = [float(r['high']) for r in rows[1:]]
        lows = [float(r['low']) for r in rows[1:]]
        avg_vol_20 = sum(float(r['vol']) for r in rows[1:21]) / 20 if len(rows) > 20 else sum(float(r['vol']) for r in rows[1:]) / len(rows[1:])
        mid_price = (max(highs) + min(lows)) / 2
        range_width = (max(highs) - min(lows)) / mid_price if mid_price > 0 else 999
        if range_width > 0.15:  # 区间太宽，不是中枢
            return False
        # 突破上限+量能
        zone_upper = mid_price + (max(highs) - min(lows)) / 2
        vol_ratio = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0
        return today_close > zone_upper and vol_ratio > 1.3
    except:
        return False


def detect_divergence(ts_code: str, trade_date: str) -> bool:
    """
    笔段背驰检测
    条件：价格创新高但RSI/MACD不创新高（顶背离）
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, close, high, low FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 60
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 40:
            return False
        closes = [float(r['close']) for r in rows]
        highs = [float(r['high']) for r in rows]
        lows = [float(r['low']) for r in rows]
        # 近20日close是否接近60日新高
        max_60 = max(closes)
        max_20 = max(closes[:20])
        if max_20 < max_60 * 0.95:  # 没创新高
            return False
        # === 条件1: RSI顶背离（收紧版）===
        gains, losses = [], []
        for i in range(13, -1, -1):
            change = closes[i-1] - closes[i] if i > 0 else 0
            if change > 0: gains.append(change); losses.append(0)
            else: gains.append(0); losses.append(-change)
        current_rsi = 50
        if gains and losses:
            avg_g = sum(gains) / 14
            avg_l = sum(losses) / 14
            if avg_l == 0: current_rsi = 100
            else: rs = avg_g / avg_l; current_rsi = 100 - 100 / (1 + rs)
        # === 条件2: MACD柱状图正在缩量（柱状图三日连续缩小）===
        # 简化EMA12/26计算
        ema12, ema26 = None, None
        # 用60条数据计算MACD
        def _ema(values, period):
            if len(values) < period: return None
            k = 2 / (period + 1)
            ema = float(values[0])
            for v in values[1:]:
                ema = v * k + ema * (1 - k)
            return ema
        # 计算最近4日的MACD柱
        macd_bars = []
        for offset in range(3, -1, -1):  # offset 3=四日前...0=当日
            segment = closes[offset:offset+60] if offset+60 <= len(closes) else closes[offset:]
            if len(segment) < 26:
                macd_bars.append(0)
                continue
            e12 = _ema(segment, 12)
            e26 = _ema(segment, 26)
            if e12 is None or e26 is None:
                macd_bars.append(0)
                continue
            dif = e12 - e26
            dea_seg = segment[:9] if len(segment) >= 9 else segment
            dea = _ema(dea_seg, 9) if len(dea_seg) >= 9 else 0
            if dea is None: dea = 0
            macd_bars.append((dif - dea) * 2)
        macd_decaying = len(macd_bars) >= 3 and macd_bars[-1] < macd_bars[-2] < macd_bars[-3]
        # 条件：MACD柱状图持续缩量
        return macd_decaying
    except:
        return False


def detect_third_buy(ts_code: str, trade_date: str) -> bool:
    """
    三买形态检测（近似）
    条件：回调≥8%但不破前低，量比放大
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT high, low, close, vol FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s AND is_valid=1
            ORDER BY trade_date DESC LIMIT 30
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if len(rows) < 21:
            return False
        today = rows[0]
        today_close = float(today['close'])
        # 近30日最高点
        highs = [float(r['high']) for r in rows]
        recent_high = max(highs)
        # 回调深度
        if recent_high <= today_close:
            return False  # 还在创新高，没有回调
        pullback_pct = (recent_high - today_close) / recent_high
        if pullback_pct < 0.08:  # 回调不够深
            return False
        # 前一个低点（30日内最低点）
        lows = [float(r['low']) for r in rows]
        prev_low = min(lows)
        pullback_low = min(lows[:len(rows)//2])  # 回调过程中最低点
        if pullback_low < prev_low * 0.95:  # 破了前低，不是三买
            return False
        # 量比
        today_vol = float(today['vol'])
        avg_vol = sum(float(r['vol']) for r in rows[1:6]) / 5 if len(rows) > 5 else 0
        if avg_vol == 0:
            return False
        vol_ratio = today_vol / avg_vol
        return today_close > sum([float(r['close']) for r in rows[1:21]]) / 20 and vol_ratio > 1.0
    except:
        return False


def apply_chanlun_layer(ts_code: str, trade_date: str, factor_scores: Dict[str, float],
                        season: str = 'summer') -> Dict:
    """
    对L3因子评分应用缠论信号确认/抑制

    Returns:
        {'factor_adjustments': {...}, 'bonus': float, 
         'signals': {'central':bool, 'divergence':bool, 'third_buy':bool}}
    """
    adj = {'f1': 1.0, 'f2': 1.0, 'f3': 1.0, 'f4': 1.0,
           'f5': 1.0, 'f6': 1.0, 'f7': 1.0}
    signals = {'central': False, 'divergence': False, 'third_buy': False}
    bonus = 0.0

    # chaos 季节跳过缠论
    if season == 'chaos':
        return {'factor_adjustments': adj, 'bonus': bonus, 'signals': signals}

    if detect_central_breakthrough(ts_code, trade_date):
        signals['central'] = True
        adj['f2'] *= 1.2
        if factor_scores.get('f6', 0) >= 4:
            bonus += 3.0

    if detect_divergence(ts_code, trade_date):
        signals['divergence'] = True
        adj['f1'] *= 0.5
        adj['f2'] = max(0.0, adj['f2'] - 0.3)  # -2分等价为-0.3倍增

    if detect_third_buy(ts_code, trade_date):
        signals['third_buy'] = True
        adj['f4'] *= 1.3
        if factor_scores.get('f4', 0) >= 3:
            bonus += 2.0

    return {'factor_adjustments': adj, 'bonus': bonus, 'signals': signals}
