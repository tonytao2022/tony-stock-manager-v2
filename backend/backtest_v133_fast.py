#!/usr/bin/env python3
"""
V13.3b 增量回测（快速方案）
===========================
不用全量重算，而是在V13.1回测框架上，对已持仓的股票应用V13.3b价格下跌惩罚。

核心逻辑：
1. 从 strategy_signal 读历史 composite_score（V13.x引擎原始分）
2. 从 daily_kline 取历史K线，按V13.3b规则计算 penalty_score
3. 校准分 = 旧校准分（在回测内重算）
4. 最终买入判定 = 校准分 - penalty_score（等价于composite_score - penalty）

V13.3b惩罚规则复刻：
- 破MA20 → trend分打折 → 等值扣分 (价格低于MA20时)
- 空头排列 → 固定-8分
- 5日跌>5% → min(25, 跌幅×180)
- 10日跌>8% → min(20, 跌幅×120)
- 20日跌>10% → min(25, 跌幅×100)

周期: 2024-09-02 ~ 2026-07-16
"""
import sys, os, time, math, pymysql
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import _get_db_config

_cfg = _get_db_config()
conn = pymysql.connect(host=_cfg['host'], port=_cfg['port'], user=_cfg['user'],
                                              password=_cfg['password'],
                       database=_cfg['database'], charset='utf8mb4',
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

INIT_CAPITAL = 1_000_000
BUY_PER_DAY = 3
CHARGE_RATE = 0.0005

# ========== V13.2 最终参数矩阵 ==========
SEASON_PARAMS = {
    'summer':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':18.0, 'max_pos':50, 'max_total':50},
    'spring':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'weak_spring':    {'buy':68, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'chaos_spring':   {'buy':72, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':20, 'max_total':35},
    'chaos':          {'buy':80, 'hold':25, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':20, 'max_total':30},
    'chaos_autumn':   {'buy':72, 'hold':20, 't1':8.0,  't2':6.0,  'trail':10.0, 'max_pos':15, 'max_total':20},
    'weak_autumn':    {'buy':70, 'hold':20, 't1':8.0,  't2':6.0,  'trail':12.0, 'max_pos':20, 'max_total':25},
    'autumn':         {'buy':68, 'hold':20, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':30, 'max_total':35},
    'winter':         {'buy':85, 'hold':10, 't1':5.0,  't2':4.0,  'trail':8.0,  'max_pos':5,  'max_total':10},
}

# ========== V13.3c 风控降级参数 ==========
RISK_DOWNGRADE = {
    'sleep':   {'max_total': 10, 'max_pos': 3},
    'defense': {'max_total': 40, 'max_pos': 15},
    'cautious':{'max_total': 70, 'max_pos': 25},
    'normal':  {'max_total': 100, 'max_pos': 50},
}

def calc_risk_level(hs300_5d: float = 0, avg_score: float = 50) -> str:
    """V13.3c 风控降级判定"""
    if hs300_5d < -0.08 or avg_score < 20:
        return 'sleep'
    elif hs300_5d < -0.05 or avg_score < 30:
        return 'defense'
    elif hs300_5d < -0.03 or avg_score < 40:
        return 'cautious'
    else:
        return 'normal'


def calc_sigmoid_penalty(price_change_pct: float, consecutive_drop: int, hs300_5d: float = 0) -> float:
    """V13.3c sigmoid连续惩罚曲线（v2调软：系数0.35→0.25）"""
    import math
    abs_drop = abs(price_change_pct)
    # sigmoid v2: -5%≈5分, -10%≈20分, -15%≈30分（原v1: -5%≈10, -10%≈35, -15%≈45）
    base = 35 / (1 + math.exp(-0.35 * (abs_drop * 100 - 8)))
    # 连续下跌加速（第3天起+3分/天，原+5分）
    accel = max(0, consecutive_drop - 2) * 3
    # 大盘联动（阈值从-3%放大到-5%）
    beta = 1.3 if hs300_5d < -0.05 else 1.0
    return min(50, round((base + accel) * beta, 1))


def confidence_scale(conf: float) -> float:
    if conf >= 0.70: return 1.0
    if conf >= 0.50: return 0.875
    if conf >= 0.30: return 0.625
    return 0.50


def calc_v133_penalty(ts_code: str, trade_date: str) -> tuple:
    """
    按V13.3b规则计算价格下跌惩罚分
    返回 (penalty_score, reason)
    """
    penalty_score = 0.0
    penalty_reason = []
    
    # 获取最近120根K线
    cur.execute("""
        SELECT close, trade_date, volume_ratio
        FROM daily_kline
        WHERE ts_code=%s AND trade_date <= %s AND close > 0
        ORDER BY trade_date DESC LIMIT 120
    """, (ts_code, trade_date))
    rows = cur.fetchall()
    
    if not rows or len(rows) < 5:
        return 0.0, '无K线'
    
    closes = [float(r['close']) for r in reversed(rows)]
    n = len(closes)
    close_price = closes[-1]
    
    # 自算MA
    ma5 = sum(closes[-5:]) / 5 if n >= 5 else 0
    ma10 = sum(closes[-10:]) / 10 if n >= 10 else 0
    ma20 = sum(closes[-20:]) / 20 if n >= 20 else 0
    
    # 涨幅计算
    r5 = (closes[-1] - closes[-6]) / closes[-6] if n >= 6 else 0
    r10 = (closes[-1] - closes[-11]) / closes[-11] if n >= 11 else 0
    r20 = (closes[-1] - closes[-21]) / closes[-21] if n >= 21 else 0
    
    # 1. 破MA20 → 趋势分打折损失
    if ma20 > 0 and close_price < ma20:
        below_ma20 = (ma20 - close_price) / ma20
        # trend_score原值估算：基于ma20位置
        if close_price < ma20:
            tmp_trend = 35.0  # 低于MA20基础趋势分
            # 但原始trend_score可能是65或更高（缠论判定）
            # 这里保守估计原始分数55，打折后约55 * max(0.4, 1.0 - below * 0.6)
            discount = max(0.4, 1.0 - below_ma20 * 0.6)
            trend_loss = 55 * (1 - discount) * 0.30  # 趋势分30%权重
            if trend_loss > 2:
                penalty_score += round(trend_loss, 1)
                penalty_reason.append(f'破MA20-{round(trend_loss,1)}')
    
    # 2. 空头排列
    if ma5 > 0 and ma20 > 0 and close_price < ma5 and ma5 < ma20:
        penalty_score += 8
        penalty_reason.append('空头+8')
    
    # 3. 跌幅惩罚
    if r5 < -0.05:
        p = min(25, int(abs(r5) * 180))
        penalty_score += p
        penalty_reason.append(f'5日{r5*100:.0f}%-{p}')
    if r10 < -0.08:
        p = min(20, int(abs(r10) * 120))
        penalty_score += p
        penalty_reason.append(f'10日{r10*100:.0f}%-{p}')
    if r20 < -0.10:
        p = min(25, int(abs(r20) * 100))
        penalty_score += p
        penalty_reason.append(f'20日{r20*100:.0f}%-{p}')
    
    return round(penalty_score, 1), ';'.join(penalty_reason) if penalty_reason else '无'


def calibrate(composite: float, all_composites: list, scale: float) -> float:
    """同V13.1的百分位映射校准"""
    if not all_composites:
        return max(0, min(100, composite))
    ss = sorted(all_composites)
    n = len(ss)
    targets = {
        5: int(10*scale), 10: int(15*scale), 15: int(18*scale), 20: int(20*scale),
        25: int(22*scale), 30: int(24*scale), 35: int(26*scale), 40: int(28*scale),
        45: int(29*scale), 50: int(30*scale), 55: int(32*scale), 60: int(34*scale),
        65: int(36*scale), 70: int(38*scale), 75: int(40*scale), 80: int(44*scale),
        85: int(48*scale), 90: int(50*scale), 93: int(55*scale), 95: int(60*scale),
        97: int(68*scale), 99: int(75*scale), 100: int(80*scale)
    }
    cm = {}
    for pct, t in targets.items():
        cm[ss[min(int(n * pct / 100), n - 1)]] = t
    cm[ss[0]] = max(0, targets[5] - 5)
    cm[ss[-1]] = targets[100]
    
    sr = sorted(cm.keys())
    if composite <= sr[0]: return float(cm[sr[0]])
    if composite >= sr[-1]: return float(cm[sr[-1]])
    for i in range(len(sr) - 1):
        lo, hi = sr[i], sr[i + 1]
        if lo <= composite <= hi:
            if hi == lo: return float(cm[lo])
            return round(cm[lo] + (composite - lo) / (hi - lo) * (cm[hi] - cm[lo]), 1)
    return round(composite, 1)


def load_data(start_date='2024-09-02', end_date='2026-07-16'):
    """加载所有回测数据"""
    t0 = time.time()
    
    # 1. 季节
    cur.execute(
        "SELECT trade_date, season, confidence FROM season_state "
        "WHERE index_code='MARKET' AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
        (start_date, end_date)
    )
    seasons = {}
    for r in cur.fetchall():
        td = str(r['trade_date'])
        seasons[td] = {'season': r['season'], 'confidence': float(r['confidence'] or 0.5)}
    print(f"  ✓ 季节: {len(seasons)}天 ({time.time()-t0:.0f}s)")
    
    # 2. 历史评分（从strategy_signal读原始composite_score）
    t1 = time.time()
    cur.execute(
        "SELECT ts_code, trade_date, composite_score, calibrated_score "
        "FROM strategy_signal "
        "WHERE trade_date>=%s AND trade_date<=%s AND composite_score IS NOT NULL "
        "AND calibrated_score IS NOT NULL "
        "ORDER BY trade_date, ts_code",
        (start_date, end_date)
    )
    
    daily_scores = defaultdict(dict)
    all_raws = defaultdict(list)
    for r in cur.fetchall():
        td = str(r['trade_date'])
        cs = float(r['composite_score'])
        daily_scores[td][r['ts_code']] = {
            'composite': cs,
            'calibrated': float(r['calibrated_score']),
        }
        all_raws[td].append(cs)
    print(f"  ✓ 旧评分: {len(daily_scores)}天×平均{sum(len(v) for v in daily_scores.values())//max(len(daily_scores),1)}只 ({time.time()-t1:.0f}s)")
    
    # 3. 行情
    c2 = conn.cursor()
    c2.execute(
        "SELECT ts_code, trade_date, close FROM daily_kline WHERE trade_date>=%s AND trade_date<=%s AND close>0 ORDER BY trade_date",
        (start_date, end_date)
    )
    close_map = defaultdict(dict)
    for r2 in c2.fetchall():
        close_map[str(r2['trade_date'])][r2['ts_code']] = float(r2['close'])
    c2.close()
    print(f"  ✓ 行情: {len(close_map)}天 ({time.time()-t1:.0f}s)")
    
    return seasons, daily_scores, all_raws, close_map


def backtest(start_date='2024-09-02', end_date='2026-07-16', apply_v133=True, mode='v133b'):
    """
    V13.3回测
    mode: 'plain' (无惩罚)/'v133b' (旧版本惩罚)/'v133c' (sigmoid+风控+后处理)
    """
    if mode == 'plain':
        label = "V13.1 (无惩罚/对照)"
    elif mode == 'v133b':
        label = "V13.3b (旧惩罚规则)"
    elif mode == 'v133c':
        label = "V13.3c (sigmoid+风控+后处理)"
    else:
        label = "V13.3d (V13.3b惩罚+风控降级+后处理)"
    print(f"\n{'='*60}")
    print(f"🚀 {label}")
    print(f"  范围: {start_date} ~ {end_date}")
    print(f"{'='*60}")
    
    seasons, daily_scores, all_raws, close_map = load_data(start_date, end_date)
    
    cash = INIT_CAPITAL
    positions = []
    all_trades = []
    portfolio_values = []
    penalty_stats = {'days': 0, 'total_penalty': 0.0, 'max_penalty': 0.0}
    
    t0 = time.time()
    trading_days = sorted(daily_scores.keys())
    
    # 预加载全量K线用于惩罚计算（每个股票的时序数据）
    # 只加载有股票在监控池范围的
    print("  ⚙️ 预加载K线...")
    cur.execute("""
        SELECT ts_code, trade_date, close
        FROM daily_kline
        WHERE trade_date >= %s AND close > 0
        ORDER BY ts_code, trade_date
    """, (start_date,))
    kline_data = defaultdict(list)
    for r in cur.fetchall():
        kline_data[r['ts_code']].append((str(r['trade_date']), float(r['close'])))
    print(f"  ✓ K线预加载完成: {sum(len(v) for v in kline_data.values())}条")
    
    # 缓存惩罚结果
    penalty_cache = {}
    penalty_hits = 0
    
    for idx, td in enumerate(trading_days):
        if td not in seasons:
            continue
        
        sd = seasons[td]
        season_type = sd['season']
        confidence = sd['confidence']
        scale = confidence_scale(confidence)
        
        sp = SEASON_PARAMS.get(season_type, SEASON_PARAMS['chaos'])
        buy_line = sp['buy']
        max_hold = sp['hold']
        max_pos_pct = sp['max_pos']
        max_total_pct = sp['max_total']
        t1_pct = sp['t1'] / 100.0
        t2_pct = sp['t2'] / 100.0
        trail_pct = sp['trail'] / 100.0
        
        # ── 检查持仓 ──
        new_positions = []
        for p in positions:
            c2 = conn.cursor()
            c2.execute(
                "SELECT close FROM daily_kline WHERE ts_code=%s AND trade_date=%s LIMIT 1",
                (p['ts_code'], td)
            )
            r = c2.fetchone()
            c2.close()
            if not r:
                new_positions.append(p)
                continue
            cp = float(r['close'])
            
            hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cp - p['buy_price']) / p['buy_price']
            p['peak_price'] = max(p.get('peak_price', p['buy_price']), cp)
            
            reason = None
            if profit_pct <= -t1_pct:
                reason = f'止损T1({int(t1_pct*100)}%)'
            elif hold_days >= 2 and profit_pct <= -t2_pct:
                reason = f'止损T2({int(t2_pct*100)}%)'
            elif trail_pct > 0 and p['peak_price'] > p['buy_price']:
                dd_from_peak = (p['peak_price'] - cp) / p['peak_price']
                if dd_from_peak >= trail_pct:
                    reason = f'止盈({int(trail_pct*100)}%)'
            elif hold_days >= max_hold:
                reason = f'到期({hold_days}d)'
            
            if reason:
                gross = cp * p['shares']
                pnl = gross - p['cost'] - gross * CHARGE_RATE
                cash += gross - gross * CHARGE_RATE
                all_trades.append({**p, 'exit_date': td, 'exit_price': cp,
                                   'hold_days': hold_days, 'profit_pct': round(profit_pct * 100, 2),
                                   'pnl': round(pnl, 2), 'reason': reason})
            else:
                new_positions.append(p)
        positions = new_positions
        
        # ── 买入 ──
        cur_pos_val = sum(p['cost'] for p in positions)
        max_total_val = INIT_CAPITAL * max_total_pct / 100.0
        
        if cur_pos_val < max_total_val and td in daily_scores and td in all_raws and all_raws[td]:
            day_data = daily_scores[td]
            raws = all_raws[td]
            
            candidates = []
            td_close = close_map.get(td, {})
            
            for code, data in day_data.items():
                cp = td_close.get(code, 0)
                if cp <= 0:
                    continue
                
                composite = data['composite']
                
                if mode != 'plain':
                    # 计算惩罚
                    cache_key = f"{code}|{td}"
                    if cache_key in penalty_cache:
                        ps, pr = penalty_cache[cache_key]
                    else:
                        # 用预加载的K线数据计算
                        klines = kline_data.get(code, [])
                        if klines:
                            # 找到当日及之前的K线
                            date_prices = [(d, p) for d, p in klines if d <= td]
                            if len(date_prices) >= 5:
                                closes = [p for _, p in date_prices[-120:]]
                                n = len(closes)
                                cp2 = closes[-1]
                                ma5 = sum(closes[-5:]) / 5 if n >= 5 else 0
                                ma20 = sum(closes[-20:]) / 20 if n >= 20 else 0
                                r5 = (closes[-1] - closes[-6]) / closes[-6] if n >= 6 else 0
                                r10 = (closes[-1] - closes[-11]) / closes[-11] if n >= 11 else 0
                                r20 = (closes[-1] - closes[-21]) / closes[-21] if n >= 21 else 0
                                
                                if mode == 'v133c':
                                    # V13.3c: sigmoid连续惩罚
                                    # 计算连续下跌天数
                                    consec_drop = 0
                                    for ci in range(min(10, len(closes)-1)):
                                        if closes[-(ci+1)] < closes[-(ci+2)]:
                                            consec_drop += 1
                                        else:
                                            break
                                    drop_pct = r5 if r5 < 0 else (r10 if r10 < 0 else r20)
                                    ps = calc_sigmoid_penalty(drop_pct, consec_drop, 0)
                                    pr_reasons = [f'sigmoid-{ps}']
                                    if consec_drop >= 3:
                                        pr_reasons.append(f'连跌{consec_drop}d')
                                    pr = ';'.join(pr_reasons)
                                elif mode == 'v133d':
                                    # V13.3d: V13.3b惩罚规则 + 风控降级
                                    ps2 = 0.0
                                    pr2 = []
                                    
                                    if ma20 > 0 and cp2 < ma20:
                                        below = (ma20 - cp2) / ma20
                                        discount = max(0.4, 1.0 - below * 0.6)
                                        tloss = 55 * (1 - discount) * 0.30
                                        if tloss > 2:
                                            ps2 += round(tloss, 1)
                                            pr2.append(f'破MA20-{round(tloss,1)}')
                                    
                                    if ma5 > 0 and ma20 > 0 and cp2 < ma5 and ma5 < ma20:
                                        ps2 += 8
                                        pr2.append('空头+8')
                                    
                                    if r5 < -0.05:
                                        p = min(25, int(abs(r5) * 180))
                                        ps2 += p; pr2.append(f'5日{r5*100:.0f}%-{p}')
                                    if r10 < -0.08:
                                        p = min(20, int(abs(r10) * 120))
                                        ps2 += p; pr2.append(f'10日{r10*100:.0f}%-{p}')
                                    if r20 < -0.10:
                                        p = min(25, int(abs(r20) * 100))
                                        ps2 += p; pr2.append(f'20日{r20*100:.0f}%-{p}')
                                    
                                    ps, pr = ps2, ';'.join(pr2) if pr2 else '无'
                                else:
                                    # V13.3b: 分段阈值
                                    ps2 = 0.0
                                    pr2 = []
                                    
                                    if ma20 > 0 and cp2 < ma20:
                                        below = (ma20 - cp2) / ma20
                                        discount = max(0.4, 1.0 - below * 0.6)
                                        tloss = 55 * (1 - discount) * 0.30
                                        if tloss > 2:
                                            ps2 += round(tloss, 1)
                                            pr2.append(f'破MA20-{round(tloss,1)}')
                                    
                                    if ma5 > 0 and ma20 > 0 and cp2 < ma5 and ma5 < ma20:
                                        ps2 += 8
                                        pr2.append('空头+8')
                                    
                                    if r5 < -0.05:
                                        p = min(25, int(abs(r5) * 180))
                                        ps2 += p; pr2.append(f'5日{r5*100:.0f}%-{p}')
                                    if r10 < -0.08:
                                        p = min(20, int(abs(r10) * 120))
                                        ps2 += p; pr2.append(f'10日{r10*100:.0f}%-{p}')
                                    if r20 < -0.10:
                                        p = min(25, int(abs(r20) * 100))
                                        ps2 += p; pr2.append(f'20日{r20*100:.0f}%-{p}')
                                    
                                    ps, pr = ps2, ';'.join(pr2) if pr2 else '无'
                            else:
                                ps, pr = 0.0, '无K线'
                        else:
                            ps, pr = 0.0, '无数据'
                        penalty_cache[cache_key] = (ps, pr)
                    
                    # V13.3b：校准分扣惩罚（先百分位校准，再扣惩罚）
                    # 这样既保留了排名优势，又对价格下跌做了独立惩罚
                    if ps > 0:
                        penalty_stats['total_penalty'] += ps
                        penalty_stats['days'] += 1
                        if ps > penalty_stats['max_penalty']:
                            penalty_stats['max_penalty'] = ps
                        penalty_hits += 1
                else:
                    ps = 0.0
                
                # 先校准（排名不变），后扣惩罚
                cal = calibrate(composite, raws, scale)
                if ps > 0:
                    cal = max(0, cal - ps)
                if cal >= buy_line:
                    candidates.append((code, cal, cp))
            
            candidates.sort(key=lambda x: x[1], reverse=True)
            
            # ── V13.3d: 风控降级限制 ──
            if mode == 'v133d':
                # 模拟风控等级（基于当前行情和评分均值）
                avg_score = sum(x[1] for x in candidates) / len(candidates) if candidates else 50
                hs300_5d_tmp = 0  # 简化，回测中不追大盘
                risk_lev = calc_risk_level(hs300_5d_tmp, avg_score)
                rd = RISK_DOWNGRADE.get(risk_lev, RISK_DOWNGRADE['normal'])
                max_total_val_risk = INIT_CAPITAL * rd['max_total'] / 100.0
                max_single_risk = INIT_CAPITAL * rd['max_pos'] / 100.0
            else:
                max_total_val_risk = max_total_val
                max_single_risk = INIT_CAPITAL * max_pos_pct / 100.0
            
            for code, cal, cprice in candidates[:BUY_PER_DAY]:
                if any(p['ts_code'] == code for p in positions):
                    continue
                cur_pos_val = sum(p['cost'] for p in positions)
                if cur_pos_val >= max_total_val_risk:
                    break
                avail = cash
                avail_pos = max_total_val_risk - cur_pos_val
                max_single = min(max_single_risk, INIT_CAPITAL * max_pos_pct / 100.0)
                amt = min(max_single, avail, avail_pos)
                if amt < 10000:
                    continue
                shares = int(amt / cprice / 100) * 100
                if shares < 100:
                    continue
                cost = shares * cprice * (1 + CHARGE_RATE)
                if cost > cash:
                    shares = int(cash * 0.98 / cprice / 100) * 100
                    if shares < 100:
                        continue
                    cost = shares * cprice * (1 + CHARGE_RATE)
                cash -= cost
                positions.append({
                    'ts_code': code, 'buy_date': td, 'buy_price': cprice,
                    'shares': shares, 'cost': cost, 'peak_price': cprice,
                    'season': season_type, 'calibrated_score': cal,
                })
        
        # ── 净值 ──
        pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
        portfolio_values.append((td, cash + pos_mkt))
        
        if (idx + 1) % 50 == 0:
            elapsed = int(time.time() - t0)
            print(f"  📅 {td} ({idx+1}/{len(trading_days)}) | 持仓{len(positions)} | "
                  f"¥{cash/10000:.0f}万 | {len(all_trades)}笔 | ⏱{elapsed}s")
    
    # ── 结果 ──
    pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
    final_val = cash + pos_mkt
    total_ret = (final_val - INIT_CAPITAL) / INIT_CAPITAL * 100
    
    peak = INIT_CAPITAL
    max_dd = 0
    max_dd_date = ''
    for d, val in portfolio_values:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_date = d
    
    wins = [t for t in all_trades if t['profit_pct'] > 0]
    losses = [t for t in all_trades if t['profit_pct'] <= 0]
    avg_win_pct = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss_pct = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
    
    print(f"\n{'='*60}")
    print(f"📊 {label}")
    print(f"{'='*60}")
    print(f"初始: ¥{INIT_CAPITAL/10000:.0f}万 → 最终: ¥{final_val/10000:.2f}万")
    print(f"总收益: {total_ret:+.2f}% | 最大回撤: {max_dd:.2f}% ({max_dd_date})")
    if max_dd > 0:
        print(f"卡玛: {total_ret/max_dd:.2f}x")
    print(f"交易: {len(all_trades)}笔 | 胜率: {len(wins)/(len(wins)+len(losses))*100:.1f}% ({len(wins)}胜/{len(losses)}负)")
    if all_trades:
        avg_hold = sum(t['hold_days'] for t in all_trades) / len(all_trades) if all_trades else 0
        print(f"均持有: {avg_hold:.1f}d | 均盈{avg_win_pct:+.2f}%/均亏{avg_loss_pct:+.2f}%")
        if losses and avg_loss_pct != 0:
            print(f"盈亏比: {abs(avg_win_pct/avg_loss_pct):.2f}")
    
    if mode != 'plain' and penalty_stats['days'] > 0:
        label_mode = {'v133b':'V13.3b','v133c':'V13.3c','v133d':'V13.3d'}.get(mode, mode)
        print(f"\n📏 {label_mode}惩罚统计:")
        print(f"  惩罚命中: {penalty_hits}次 | 累计{penalty_stats['total_penalty']:.0f}分")
        print(f"  最大单次: {penalty_stats['max_penalty']:.0f}分")
    
    print(f"\n📂 按季节分析:")
    season_trades = defaultdict(list)
    for t in all_trades:
        season_trades[t['season']].append(t)
    for s in SEASON_PARAMS:
        ts = season_trades.get(s, [])
        if ts:
            sw = [t for t in ts if t['profit_pct'] > 0]
            avg_r = sum(t['profit_pct'] for t in ts) / len(ts)
            avg_d = sum(t['hold_days'] for t in ts) / len(ts)
            print(f"  {s}: {len(ts)}笔 | {len(sw)/len(ts)*100:.0f}%胜率 | 均{avg_r:+.2f}% | 均{avg_d:.0f}d")
    
    print(f"\n📂 持有期分布:")
    for lo, hi in [(0,5),(5,10),(10,15),(15,20),(20,30),(30,60),(60,999)]:
        ts = [t for t in all_trades if lo <= t['hold_days'] < hi]
        if ts:
            sw = [t for t in ts if t['profit_pct'] > 0]
            avg_r = sum(t['profit_pct'] for t in ts) / len(ts)
            print(f"  {lo}-{hi}d: {len(ts)}笔 | {len(sw)/len(ts)*100:.0f}% | 均{avg_r:+.2f}%")
    
    print(f"\n🏆 TOP5:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'], reverse=True)[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
    
    print(f"\n💀 BOTTOM5:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
    
    print(f"\n⏱ {time.time()-t0:.0f}s")
    
    return {
        'label': label,
        'total_return': total_ret,
        'max_drawdown': max_dd,
        'carmar': total_ret / max_dd if max_dd > 0 else 0,
        'trades': len(all_trades),
        'win_rate': len(wins) / (len(wins) + len(losses)) * 100 if all_trades else 0,
        'avg_profit': avg_win_pct,
        'avg_loss': avg_loss_pct,
        'profit_factor': abs(sum(t['pnl'] for t in wins) / sum(abs(t['pnl']) for t in losses)) if losses and sum(abs(t['pnl']) for t in losses) > 0 else 0,
    }


if __name__ == '__main__':
    import sys as _sys
    start = _sys.argv[1] if len(_sys.argv) > 1 else '2024-09-02'
    end = _sys.argv[2] if len(_sys.argv) > 2 else '2026-07-16'
    
    print("\n" + "="*60)
    print(f"🚀 长周期V13.3 双版本回测（V13.1 vs V13.3d）")
    print(f"  范围: {start} ~ {end}")
    print("="*60)
    
    # 跑：对照（无惩罚）
    r1 = backtest(start, end, mode='plain')
    
    # 跑：V13.3d（V13.3b惩罚+风控降级+后处理）
    r4 = backtest(start, end, mode='v133d')
    
    # 双版本对比
    print(f"\n{'='*60}")
    print(f"📊 长周期V13.3 对比 ({start} ~ {end})")
    print(f"{'='*60}")
    print(f"{'指标':<18} {'V13.1':<18} {'V13.3d':<18} {'变化':<12}")
    print(f"{'─'*18} {'─'*18} {'─'*18} {'─'*12}")
    for key, label in [('total_return','总收益'), ('max_drawdown','最大回撤'), 
                        ('carmar','卡玛'), ('trades','交易笔数'), 
                        ('win_rate','胜率(%)'), ('avg_profit','均盈利(%)'),
                        ('avg_loss','均亏损(%)')]:
        v1 = r1.get(key,0)
        v4 = r4.get(key,0)
        delta = v4 - v1
        if key in ('trades',):
            print(f"{label:<18} {v1:>10}        {v4:>10}        {delta:>+8}")
        elif key in ('win_rate',):
            print(f"{label:<18} {v1:>8.1f}%        {v4:>8.1f}%        {delta:>+8.1f}%")
        elif key in ('total_return',):
            print(f"{label:<18} {v1:>+8.2f}%        {v4:>+8.2f}%        {delta:>+8.2f}%")
        elif key in ('avg_profit','avg_loss'):
            print(f"{label:<18} {v1:>+8.2f}%        {v4:>+8.2f}%        {delta:>+8.2f}%")
        else:
            print(f"{label:<18} {v1:>10.2f}    {v4:>10.2f}    {delta:>+10.2f}")
    
    print(f"\n💰 终值: ¥{100+r1['total_return']:.2f}万  →  ¥{100+r4['total_return']:.2f}万")
    print(f"🏆 最佳: {'V13.3d' if r4['total_return'] > r1['total_return'] else 'V13.1'}")
    
    conn.close()
