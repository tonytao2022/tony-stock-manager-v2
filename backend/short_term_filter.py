"""
P1 短期信号过滤层 v2.0 — P6引擎集成版
=============================================
定位：叠加在P6评分之上的前瞻式短期健康度
不依赖买入日期，直接基于最新数据给出短期买入适宜度

输出：short_term_score (0~100)
  ≥70 = 健康（买入加持）
  50~69 = 中性（不干预）
  30~49 = 预警（降低仓位建议）
  <30  = 危险（建议跳过买入）

4维度 (独立于P6大周期评分):
  a) 资金惯性(30%) — 近3日主力净流入趋势
  b) 量价健康(25%) — 放量涨+缩量跌=健康
  c) 超买安全(25%) — RSI位置+ATR波动 
  d) 短期动量(20%) — 5日/10日均线位置
"""
import logging
logger = logging.getLogger('short_term_filter')

from db_config import get_connection


def calc_short_term_score(ts_code: str, trade_date: str) -> dict:
    """
    计算单只股票的短期健康度
    返回: 4个细分维度 + 综合分 + 操作建议
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # 获取近20日K线 + 技术指标
        cur.execute("""
            SELECT d.close, d.change_pct, d.vol, d.trade_date,
                   d.volume_ratio, d.turnover_rate,
                   t.ma_5, t.ma_10, t.ma_20,
                   t.rsi_14, t.atr_14
            FROM daily_kline d
            LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
            WHERE d.ts_code=%s AND d.trade_date <= %s
            ORDER BY d.trade_date DESC LIMIT 20
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        
        if not rows or len(rows) < 5:
            conn.close()
            return _neutral("数据不足")
        
        latest = rows[0]
        
        # ─── a) 资金惯性 (30%) ───
        capital_score = _calc_capital_inertia(ts_code, trade_date, cur)
        
        # ─── b) 量价健康 (25%) ───
        volume_score = _calc_volume_health(rows)
        
        # ─── c) 超买安全 (25%) ───
        overbought_score = _calc_overbought_safety(rows)
        
        # ─── d) 短期动量 (20%) ───
        momentum_score = _calc_short_momentum(rows)
        
        conn.close()
        
        # 综合 (MAY建议: 资金惯性权重提升到45%)
        composite = (capital_score * 0.45 + volume_score * 0.25 +
                     overbought_score * 0.05 + momentum_score * 0.25)
        composite = max(0, min(100, round(composite, 1)))
        
        # 操作建议
        if composite >= 70:
            action = 'pass'
            label = '✅ 短期健康'
        elif composite >= 50:
            action = 'pass'
            label = '➖ 短期中性'
        elif composite >= 30:
            action = 'caution'
            label = '⚠️ 短期预警'
        else:
            action = 'block'
            label = '🚫 短期危险'
        
        return {
            'short_term_score': composite,
            'capital_inertia': round(capital_score, 1),
            'volume_health': round(volume_score, 1),
            'overbought_safety': round(overbought_score, 1),
            'short_momentum': round(momentum_score, 1),
            'action': action,
            'label': label,
        }
    except Exception as e:
        logger.error(f'[STF] {ts_code} 计算失败: {e}')
        return _neutral("计算异常")


def _neutral(reason=""):
    return {
        'short_term_score': 50.0,
        'capital_inertia': 50.0,
        'volume_health': 50.0,
        'overbought_safety': 50.0,
        'short_momentum': 50.0,
        'action': 'pass',
        'label': '➖ 数据不足',
        'reason': reason,
    }


def _calc_capital_inertia(ts_code: str, trade_date: str, cur=None) -> float:
    """
    资金惯性 (30%)
    基于moneyflow大单/特大单近5日净流入
    """
    if cur is None:
        conn = get_connection()
        cur = conn.cursor()
        need_close = True
    else:
        need_close = False
    
    try:
        cur.execute("""
            SELECT
                SUM(net_mf_amount) as mf_5d,
                SUM(buy_lg_amount - sell_lg_amount) as lg_net_5d,
                SUM(buy_elg_amount - sell_elg_amount) as elg_net_5d,
                AVG(COALESCE(net_mf_amount, 0)) as mf_avg
            FROM moneyflow
            WHERE ts_code=%s AND trade_date <= %s 
              AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
        """, (ts_code, trade_date, trade_date))
        row = cur.fetchone()
        
        score = 50  # 默认中性
        
        if row and row['mf_5d'] is not None:
            mf_5d = float(row['mf_5d'])
            lg_net = float(row['lg_net_5d'] or 0)
            elg_net = float(row['elg_net_5d'] or 0)
            
            # 净流入分级 (单位:万元)
            if mf_5d > 20000:      # +2亿 → 很强
                score = 85
            elif mf_5d > 10000:    # +1亿~2亿
                score = 75
            elif mf_5d > 5000:     # +5000万~1亿
                score = 65
            elif mf_5d > 0:        # 净流入
                score = 55
            elif mf_5d > -5000:    # 小幅流出
                score = 40
            elif mf_5d > -10000:   # 中幅流出
                score = 25
            else:                   # 大幅流出
                score = 15
            
            # 主力方向修正：大单+特大单 vs 普通单
            smart_net = lg_net + elg_net
            total_trade = abs(smart_net) + abs(mf_5d - smart_net) + 1
            smart_ratio = smart_net / total_trade if total_trade > 0 else 0
            
            if smart_ratio > 0.3:          # 主力主导流入
                score = min(100, score + 10)
            elif smart_ratio < -0.3:        # 主力主导流出
                score = max(0, score - 10)
            
            # 趋势一致性：连续3日净流入加分
            cur.execute("""
                SELECT net_mf_amount FROM moneyflow
                WHERE ts_code=%s AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 3
            """, (ts_code, trade_date))
            recent = cur.fetchall()
            if len(recent) >= 3:
                pos_days = sum(1 for r in recent if float(r['net_mf_amount'] or 0) > 0)
                if pos_days >= 2:
                    score = min(100, score + 5)
                elif pos_days <= 1:
                    score = max(0, score - 5)
        
        return max(0, min(100, score))
    except Exception as e:
        print(f"  ⚠️ short_term_filter calc failed: {e}")
        return 50
    finally:
        if need_close:
            cur.close()
            conn.close()


def _calc_volume_health(rows: list) -> float:
    """
    量价健康 (25%)
    放量涨+缩量跌=健康
    """
    if len(rows) < 5:
        return 50
    
    score = 50
    up_with_vol = 0
    down_with_low_vol = 0
    total_days = min(10, len(rows))
    
    for i in range(total_days):
        r = rows[i]
        chg = float(r.get('change_pct') or 0)
        vol = float(r.get('vol') or 0)
        vol_ratio = float(r.get('volume_ratio') or 1.0)
        
        # 放量上涨 +1分
        if chg > 2 and vol_ratio > 1.2:
            score += 3
            up_with_vol += 1
        # 缩量上涨 +1分（筹码锁定好）
        elif chg > 1 and vol_ratio < 0.8:
            score += 1
        # 放量下跌 -3分（主力出逃）
        elif chg < -2 and vol_ratio > 1.3:
            score -= 4
            down_with_low_vol -= 1
        # 缩量下跌 +1分（正常回调）
        elif chg < -2 and vol_ratio < 0.7:
            score += 2
        # 平量小幅波动
        else:
            pass
    
    return max(0, min(100, score))


def _calc_overbought_safety(rows: list) -> float:
    """
    超买安全 (25%)
    RSI在安全区间 + 近期无剧烈拉升
    """
    if not rows:
        return 50
    
    latest = rows[0]
    rsi = float(latest.get('rsi_14') or 50)
    atr = float(latest.get('atr_14') or 0)
    close = float(latest.get('close') or 0)
    atr_pct = (atr / close * 100) if close > 0 else 2.0
    
    # RSI区间评分
    if 40 <= rsi <= 60:        # 最安全区间
        score = 75
    elif 30 <= rsi < 40:       # 超卖区（反而是机会）
        score = 70
    elif 60 < rsi <= 70:       # 偏热
        score = 55
    elif rsi < 30:              # 极度超卖
        score = 80
    elif 70 < rsi <= 80:       # 超买
        score = 35
    else:                       # rsi > 80 极度超买
        score = 20
    
    # ATR波动率修正：波动太大=风险高
    if atr_pct > 5:             # 极端波动
        score = max(10, score - 20)
    elif atr_pct > 3:           # 高波动
        score = max(20, score - 10)
    elif atr_pct < 1:           # 低波动（稳定）
        score = min(90, score + 5)
    
    # 近期涨幅检查：如果3日累计涨幅>12%，超买风险溢价
    if len(rows) >= 4:
        chg_3d = sum(float(rows[i].get('change_pct') or 0) for i in range(min(3, len(rows))))
        if chg_3d > 12:
            score = max(10, score - 15)
        elif chg_3d > 8:
            score = max(20, score - 8)
    
    return max(0, min(100, score))


def _calc_short_momentum(rows: list) -> float:
    """
    短期动量 (20%)
    股价与5日/10日均线的关系
    """
    if len(rows) < 10:
        return 50
    
    latest = rows[0]
    close = float(latest.get('close') or 0)
    ma5 = float(latest.get('ma_5') or 0)
    ma10 = float(latest.get('ma_10') or 0)
    ma20 = float(latest.get('ma_20') or 0)
    
    score = 50
    
    # 均线关系
    if ma5 > 0 and close >= ma5:
        score += 15  # 在5日均线上方
    elif ma5 > 0 and close < ma5:
        score -= 10  # 跌破5日均线
    
    if ma10 > 0 and close >= ma10:
        score += 10  # 在10日均线上方
    elif ma10 > 0 and close < ma10:
        score -= 15  # 跌破10日均线（更严重）
    
    # 短期趋势: 5日线斜率
    if len(rows) >= 6:
        closes_5 = [float(rows[i].get('close') or 0) for i in range(5)]
        # 2日斜率 = 最新close vs 5日前close
        pct_5d = (closes_5[0] - closes_5[-1]) / closes_5[-1] if closes_5[-1] > 0 else 0
        if pct_5d > 5:
            score += 8
        elif pct_5d > 2:
            score += 4
        elif pct_5d < -5:
            score -= 10
        elif pct_5d < -2:
            score -= 5
    
    # 均线多头排列
    if ma5 > 0 and ma10 > 0 and ma20 > 0:
        if ma5 > ma10 > ma20:
            score += 8  # 趋势良好
        elif ma5 < ma10 < ma20:
            score -= 8  # 趋势走坏
    
    return max(0, min(100, score))


def batch_short_term_score(ts_codes: list, trade_date: str) -> dict:
    """
    批量计算短期健康度
    返回: {ts_code: {short_term_score, ...}}
    """
    results = {}
    for code in ts_codes:
        try:
            r = calc_short_term_score(code, trade_date)
            results[code] = r
        except Exception as e:
            print(f"  ⚠️ {code} short_term 评分失败: {e}")
            results[code] = _neutral()
    return results
