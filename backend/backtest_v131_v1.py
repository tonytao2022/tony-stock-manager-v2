#!/usr/bin/env python3
"""
V13.1-v1 基准版回测
========================
不用全量重算P6评分，而是在回测中：
1. 从strategy_signal读历史 composite_score（原始分）
2. 从season_state读当日confidence
3. 在回测内重算置信度动态校准后的 calibrated_score
4. 按V13.1基准版参数（买入线/持有期/止损/仓位）执行交易模拟

周期: 2024-09-02 ~ 2026-07-04
数据源: strategy_signal + season_state + daily_kline
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

# ========== V13.1基准版完整参数矩阵 ==========
SEASON_PARAMS = {
    'summer':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':18.0, 'max_pos':50, 'max_total':50},
    'spring':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'weak_spring':    {'buy':68, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'chaos_spring':   {'buy':72, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':20, 'max_total':35},
    'chaos':          {'buy':70, 'hold':25, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':20, 'max_total':30},
    'chaos_autumn':   {'buy':72, 'hold':20, 't1':8.0,  't2':6.0,  'trail':10.0, 'max_pos':15, 'max_total':20},
    'weak_autumn':    {'buy':70, 'hold':20, 't1':8.0,  't2':6.0,  'trail':12.0, 'max_pos':20, 'max_total':25},
    'autumn':         {'buy':68, 'hold':20, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':30, 'max_total':35},
    'winter':         {'buy':85, 'hold':10, 't1':5.0,  't2':4.0,  'trail':8.0,  'max_pos':5,  'max_total':10},
}

def confidence_scale(conf: float) -> float:
    """置信度→缩放系数"""
    if conf >= 0.70: return 1.0
    if conf >= 0.50: return 0.875
    if conf >= 0.30: return 0.625
    return 0.50

def calibrate_score(composite: float, all_composites: list, scale: float) -> float:
    """在回测中重算校准分（同P6引擎的百分位映射逻辑）"""
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


def load_data(start_date='2024-09-02', end_date='2026-07-04'):
    """加载回测需要的所有数据"""
    t0 = time.time()
    
    # 1. 季节+置信度
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
    
    # 2. 所有交易日的评分（只取有composite_score的记录）
    t1 = time.time()
    cur.execute(
        "SELECT ts_code, trade_date, composite_score "
        "FROM strategy_signal "
        "WHERE trade_date>=%s AND trade_date<=%s AND composite_score IS NOT NULL "
        "ORDER BY trade_date, ts_code",
        (start_date, end_date)
    )
    
    daily_scores = defaultdict(dict)  # td -> {ts_code: {composite, close, season}}
    all_raws = defaultdict(list)      # td -> [composite, ...]
    for r in cur.fetchall():
        td = str(r['trade_date'])
        cs = float(r['composite_score'])
        daily_scores[td][r['ts_code']] = {
            'composite': cs,
        }
        all_raws[td].append(cs)
    print(f"  ✓ 评分: {len(daily_scores)}天×平均{sum(len(v) for v in daily_scores.values())//max(len(daily_scores),1)}只 ({time.time()-t1:.0f}s)")
    
    print(f"  ✓ 评分: {len(daily_scores)}天×平均{sum(len(v) for v in daily_scores.values())//max(len(daily_scores),1)}只 ({time.time()-t1:.0f}s)")
    
    # 3. 行情数据（预加载所有close）
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


def backtest(start_date='2024-09-02', end_date='2026-07-04'):
    print(f"\n🚀 V13.1-v1 基准版回测: {start_date} ~ {end_date}")
    print(f"{'='*55}")
    
    seasons, daily_scores, all_raws, close_map = load_data(start_date, end_date)
    
    cash = INIT_CAPITAL
    positions = []
    all_trades = []
    portfolio_values = []
    
    t0 = time.time()
    
    # 交易日排序
    trading_days = sorted(daily_scores.keys())
    print(f"  ✓ 交易日: {len(trading_days)}天")
    
    for idx, td in enumerate(trading_days):
        # 跳过无季节数据的交易日
        if td not in seasons:
            continue
        
        sd = seasons[td]
        season_type = sd['season']
        confidence = sd['confidence']
        scale = confidence_scale(confidence)
        
        # 取该季节参数
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
            # 从daily_kline取当日收盘价
            cur.execute(
                "SELECT close FROM daily_kline WHERE ts_code=%s AND trade_date=%s LIMIT 1",
                (p['ts_code'], td)
            )
            r = cur.fetchone()
            if not r:
                new_positions.append(p)
                continue
            cp = float(r['close'])
            
            hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cp - p['buy_price']) / p['buy_price']
            p['peak_price'] = max(p.get('peak_price', p['buy_price']), cp)
            
            reason = None
            # T1止损（首日标准止损）
            if profit_pct <= -t1_pct:
                reason = f'止损T1({int(t1_pct*100)}%)'
            # T2止损（次日+更严格）
            elif hold_days >= 2 and profit_pct <= -t2_pct:
                reason = f'止损T2({int(t2_pct*100)}%)'
            # 移动止盈
            elif trail_pct > 0 and p['peak_price'] > p['buy_price']:
                dd_from_peak = (p['peak_price'] - cp) / p['peak_price']
                if dd_from_peak >= trail_pct:
                    reason = f'止盈({int(trail_pct*100)}%)'
            # 持有期到期
            elif hold_days >= max_hold:
                reason = f'到期({hold_days}d)'
            
            if reason:
                gross = cp * p['shares']
                pnl = gross - p['cost'] - gross * CHARGE_RATE
                cash += gross - gross * CHARGE_RATE
                all_trades.append({
                    **p,
                    'exit_date': td,
                    'exit_price': cp,
                    'hold_days': hold_days,
                    'profit_pct': round(profit_pct * 100, 2),
                    'pnl': round(pnl, 2),
                    'reason': reason,
                })
            else:
                new_positions.append(p)
        positions = new_positions
        
        # ── 买入 ──
        cur_pos_val = sum(p['cost'] for p in positions)
        max_total_val = INIT_CAPITAL * max_total_pct / 100.0
        
        if cur_pos_val < max_total_val and td in daily_scores and td in all_raws and all_raws[td]:
            day_data = daily_scores[td]
            raws = all_raws[td]
            
            # 计算所有票的校准分
            candidates = []
            td_close = close_map.get(td, {})
            for code, data in day_data.items():
                cal = calibrate_score(data['composite'], raws, scale)
                cp = td_close.get(code, 0)
                if cal >= buy_line and cp > 0:
                    candidates.append((code, cal, cp))
            
            candidates.sort(key=lambda x: x[1], reverse=True)
            
            for code, cal, cprice in candidates[:BUY_PER_DAY]:
                if any(p['ts_code'] == code for p in positions):
                    continue
                    
                cur_pos_val = sum(p['cost'] for p in positions)
                if cur_pos_val >= max_total_val:
                    break
                
                avail = cash
                avail_pos = max_total_val - cur_pos_val
                max_single = INIT_CAPITAL * max_pos_pct / 100.0
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
                    'ts_code': code,
                    'buy_date': td,
                    'buy_price': cprice,
                    'shares': shares,
                    'cost': cost,
                    'peak_price': cprice,
                    'season': season_type,
                    'calibrated_score': cal,
                })
        
        # ── 净值计算 ──
        pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
        portfolio_values.append((td, cash + pos_mkt))
        
        if (idx + 1) % 50 == 0:
            elapsed = int(time.time() - t0)
            print(f"  📅 {td} ({idx+1}/{len(trading_days)}) | 持仓{len(positions)} | ¥{cash/10000:.0f}万 | {len(all_trades)}笔 | {elapsed}s")
    
    # ── 结果计算 ──
    pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
    final_val = cash + pos_mkt
    total_ret = (final_val - INIT_CAPITAL) / INIT_CAPITAL * 100
    
    peak = INIT_CAPITAL
    max_dd = 0
    for _, val in portfolio_values:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        if dd > max_dd:
            max_dd = dd
    
    wins = [t for t in all_trades if t['profit_pct'] > 0]
    losses = [t for t in all_trades if t['profit_pct'] <= 0]
    
    print(f"\n{'='*55}")
    print(f"📊 V13.1-v1 基准版回测 ({start_date} ~ {end_date})")
    print(f"{'='*55}")
    print(f"初始: ¥{INIT_CAPITAL/10000:.0f}万 → 最终: ¥{final_val/10000:.0f}万")
    print(f"总收益: {total_ret:+.2f}% | 最大回撤: {max_dd:.2f}% | 卡玛: {total_ret/max_dd:.2f}x" if max_dd else f"总收益: {total_ret:+.2f}%")
    print(f"交易: {len(all_trades)}笔 | 胜率: {len(wins)/(len(wins)+len(losses))*100:.1f}% ({len(wins)}胜/{len(losses)}负)")
    if all_trades:
        avg_pnl = sum(t['pnl'] for t in all_trades) / len(all_trades)
        avg_hold = sum(t['hold_days'] for t in all_trades) / len(all_trades)
        print(f"总盈亏: ¥{sum(t['pnl'] for t in all_trades):.0f} | 平均: ¥{avg_pnl:.0f} | 均持有: {avg_hold:.1f}d")
        if wins and losses:
            avg_win = sum(t['pnl'] for t in wins) / len(wins)
            avg_loss = sum(t['pnl'] for t in losses) / len(losses)
            if avg_loss != 0:
                print(f"盈亏比: {abs(avg_win/avg_loss):.2f}")
    
    # 季节分析
    print(f"\n📂 按季节分析:")
    season_trades = defaultdict(list)
    for t in all_trades:
        season_trades[t['season']].append(t)
    for s in SEASON_PARAMS:
        ts = season_trades.get(s, [])
        if ts:
            sw = [t for t in ts if t['profit_pct'] > 0]
            print(f"  {s}: {len(ts)}笔 | {len(sw)/len(ts)*100:.0f}%胜率 | 均{sum(t['profit_pct'] for t in ts)/len(ts):+.2f}% | 均{sum(t['hold_days'] for t in ts)/len(ts):.0f}d")
    
    # 持有期分析
    print(f"\n📂 持有期分布:")
    for lo, hi in [(0,5),(5,10),(10,15),(15,20),(20,30),(30,60),(60,999)]:
        ts = [t for t in all_trades if lo <= t['hold_days'] < hi]
        if ts:
            sw = [t for t in ts if t['profit_pct'] > 0]
            print(f"  {lo}-{hi}d: {len(ts)}笔 | {len(sw)/len(ts)*100:.0f}% | 均{sum(t['profit_pct'] for t in ts)/len(ts):+.2f}%")
    
    print(f"\n🏆 TOP5:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'], reverse=True)[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
    
    print(f"\n💀 BOTTOM5:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
    
    conn.close()
    print(f"\n⏱ {time.time()-t0:.0f}s")


if __name__ == '__main__':
    import sys as _sys
    if len(_sys.argv) > 2:
        backtest(_sys.argv[1], _sys.argv[2])
    else:
        backtest('2024-09-02', '2026-07-04')
