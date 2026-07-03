#!/usr/bin/env python3
"""
May & Main 最终优化方案 V2 全量回测（高性能版）
================================================
先把所有数据预取到内存字典，再跑模拟交易，避免逐日查询DB。

新引擎动量轨道：
  趋势分30% + 位置因子15% + 结构分10% + 动量因子25%
  + 大单净流入15% + 融资融券10% + 换手率过滤器

运行: python3 backtest_final_v2.py
"""
import sys, os, json, time, math
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymysql

MYSQL_PWD = 'iXve1rVBXfdA4tL9'

def get_conn():
    return pymysql.connect(
        host='localhost', user='debian-sys-maint', password='iXve1rVBXfdA4tL9',
        database='stock_db_v2', charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )

# ============================================================
# 数据预取
# ============================================================
def load_all_data():
    """预取所有需要的数据到内存"""
    conn = get_conn()
    cur = conn.cursor()
    data = {}
    
    print("预取数据...")
    
    # 1. 回测池
    cur.execute("SELECT ts_code, name FROM backtest_pool")
    data['pool'] = {r['ts_code']: r.get('name','') for r in cur.fetchall()}
    
    # 2. 全部交易日
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12' ORDER BY trade_date")
    data['trade_dates'] = [r['trade_date'].strftime('%Y-%m-%d') for r in cur.fetchall()]
    
    # 3. K线（按股票分组，按日期排序）
    cur.execute("""SELECT ts_code, trade_date, close, vol 
                   FROM daily_kline_qfq 
                   WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12'
                   ORDER BY ts_code, trade_date""")
    klines = cur.fetchall()
    kline_map = defaultdict(dict)
    for r in klines:
        kline_map[r['ts_code']][r['trade_date'].strftime('%Y-%m-%d')] = {
            'close': float(r['close']),
            'vol': float(r['vol']) if r['vol'] else 0,
        }
    data['kline'] = dict(kline_map)
    print(f"  K线: {len(klines)}条, {len(kline_map)}只")
    
    # 4. 趋势分（strategy_signal）
    cur.execute("""SELECT ts_code, trade_date, trend_score, structure_score, momentum_score
                   FROM strategy_signal WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12'
                   ORDER BY ts_code, trade_date""")
    sigs = cur.fetchall()
    sig_map = defaultdict(dict)
    for r in sigs:
        td = r['trade_date'].strftime('%Y-%m-%d')
        sig_map[r['ts_code']][td] = {
            'trend': float(r['trend_score']) if r['trend_score'] else 50,
            'structure': float(r['structure_score']) if r['structure_score'] else 50,
            'momentum': float(r['momentum_score']) if r['momentum_score'] else 50,
        }
    data['signal'] = dict(sig_map)
    print(f"  策略信号: {len(sigs)}条, {len(sig_map)}只")
    
    # 5. 资金流向
    cur.execute("""SELECT ts_code, trade_date,
                   COALESCE(buy_lg_amount,0)+COALESCE(buy_elg_amount,0)-COALESCE(sell_lg_amount,0)-COALESCE(sell_elg_amount,0) as net_flow
                   FROM moneyflow 
                   WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12'
                   ORDER BY ts_code, trade_date""")
    flows = cur.fetchall()
    flow_map = defaultdict(dict)
    for r in flows:
        flow_map[r['ts_code']][r['trade_date'].strftime('%Y-%m-%d')] = float(r['net_flow'])
    data['flow'] = dict(flow_map)
    print(f"  资金流向: {len(flows)}条, {len(flow_map)}只")
    
    # 6. 融资融券
    cur.execute("""SELECT ts_code, trade_date, rzmre, rzrqye
                   FROM margin_detail 
                   WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12'
                   AND rzmre IS NOT NULL
                   ORDER BY ts_code, trade_date""")
    margins = cur.fetchall()
    margin_map = defaultdict(dict)
    for r in margins:
        margin_map[r['ts_code']][r['trade_date'].strftime('%Y-%m-%d')] = {
            'rzmre': float(r['rzmre']),
            'rzrqye': float(r['rzrqye']) if r['rzrqye'] else 0,
        }
    data['margin'] = dict(margin_map)
    print(f"  融资融券: {len(margins)}条, {len(margin_map)}只")
    
    # 7. 季节状态
    cur.execute("SELECT trade_date, season FROM season_state ORDER BY trade_date")
    seasons = cur.fetchall()
    season_map = {}
    for r in seasons:
        season_map[r['trade_date'].strftime('%Y-%m-%d')] = r['season']
    data['season'] = season_map
    print(f"  季节状态: {len(seasons)}条")
    
    cur.close()
    conn.close()
    print(f"预取完成")
    return data


# ============================================================
# 季节参数
# ============================================================
SEASON_PARAMS = {
    'summer':  {'buy_line': 72, 'max_hold': 60, 't1_stop': 12, 't2_stop': 9, 
                'p4_threshold': 55, 'p4_extend': 15, 'trailing_stop': 18, 't2_enabled': True},
    'autumn':  {'buy_line': 75, 'max_hold': 25, 't1_stop': 8, 't2_stop': 6,
                'p4_threshold': 65, 'p4_extend': 5, 'trailing_stop': 12, 't2_enabled': True},
    'spring':  {'buy_line': 70, 'max_hold': 20, 't1_stop': 8, 't2_stop': 6,
                'p4_threshold': 60, 'p4_extend': 5, 'trailing_stop': 12, 't2_enabled': True},
    'winter':  {'buy_line': 85, 'max_hold': 10, 't1_stop': 5, 't2_stop': 4,
                'p4_threshold': 999, 'p4_extend': 0, 'trailing_stop': 8, 't2_enabled': False},
    'chaos':   {'buy_line': 75, 'max_hold': 25, 't1_stop': 10, 't2_stop': 8,
                'p4_threshold': 65, 'p4_extend': 5, 'trailing_stop': 12, 't2_enabled': False},
}


def get_season(td, season_map):
    """取最近季节"""
    if td in season_map:
        return season_map[td]
    # 找最近的一个
    for d in sorted(season_map.keys(), reverse=True):
        if d <= td:
            return season_map[d]
    return 'chaos'


# ============================================================
# 因子计算（全内存）
# ============================================================
def score_stock_mem(code, td, data):
    """基于内存数据的新引擎评分"""
    k = data['kline']
    flow = data['flow']
    margin = data['margin']
    signal = data['signal']
    
    # --- 趋势分（优先用strategy_signal，没有则默认50）---
    trend = 50
    if code in signal and td in signal[code]:
        trend = signal[code][td]['trend']
    
    # --- 结构分 ---
    struct = 50
    if code in signal and td in signal[code]:
        struct = signal[code][td]['structure'] * 2 if signal[code][td]['structure'] else 50
    if struct <= 50:
        # 从signal降级用默认
        struct = 50
    
    # --- 动量因子 ---
    momentum = 50
    if code in signal and td in signal[code]:
        momentum = signal[code][td]['momentum']
    if momentum <= 50:
        momentum = 50
    
    # --- 位置因子：250日均线偏离 ---
    pos_score = 50
    pos_dev = 0
    if code in k:
        closes = sorted([d for d in k[code].keys() if d <= td])
        if len(closes) >= 250:
            last_close = k[code][closes[-1]]['close']
            ma250 = sum(k[code][closes[-i-1]]['close'] for i in range(250)) / 250
            dev = (last_close - ma250) / ma250
            pos_score = (dev + 0.30) / 0.60 * 100
            pos_score = max(0, min(100, pos_score))
            pos_dev = dev
    
    # --- 大单净流入：近5日累计 ---
    mf_score = 50
    if code in flow:
        flow_dates = [d for d in sorted(flow[code].keys()) if d <= td]
        if len(flow_dates) >= 5:
            net_5d = sum(flow[code][d] for d in flow_dates[-5:])
            mf_score = (net_5d + 500) / (50000 + 500) * 100
            mf_score = max(0, min(100, mf_score))
        elif len(flow_dates) > 0:
            net = sum(flow[code][d] for d in flow_dates)
            mf_score = (net + 500) / (50000 + 500) * 100
            mf_score = max(0, min(100, mf_score))
    
    # --- 融资融券：近5日融资买入均值 ---
    margin_score = 50
    if code in margin:
        m_dates = [d for d in sorted(margin[code].keys()) if d <= td]
        if len(m_dates) >= 5:
            avg_rz = sum(margin[code][d]['rzmre'] for d in m_dates[-5:]) / 5
            margin_score = math.log10(max(1, avg_rz / 10000)) / 5 * 100
            margin_score = max(0, min(100, margin_score))
    
    # --- 综合评分 ---
    composite = (trend * 0.30 + pos_score * 0.15 + struct * 0.10 +
                 momentum * 0.25 + mf_score * 0.15 + margin_score * 0.10)
    
    # --- 换手率过滤器（量比） ---
    vr = 1.0
    if code in k:
        closes = sorted([d for d in k[code].keys() if d <= td])
        if len(closes) >= 2:
            idx = len(closes) - 1
            today_vol = k[code][closes[idx]]['vol']
            if today_vol > 0 and idx >= 20:
                vols_20 = [k[code][closes[idx-j-1]]['vol'] for j in range(min(20, idx))]
                avg_vol = sum(vols_20) / len(vols_20) if vols_20 else 1
                vr = today_vol / avg_vol if avg_vol > 0 else 1.0
    
    if vr < 0.3:
        composite *= 0.95
    elif vr > 5.0:
        composite *= 0.90
    elif 0.8 <= vr <= 2.0:
        composite *= 1.02
    
    composite = max(0, min(100, composite))
    
    return round(composite, 2), {
        'trend': round(trend, 1),
        'position': round(pos_score, 1),
        'pos_dev': round(pos_dev, 4),
        'structure': round(struct, 1),
        'momentum': round(momentum, 1),
        'moneyflow': round(mf_score, 1),
        'margin': round(margin_score, 1),
        'vol_ratio': round(vr, 2),
    }


# ============================================================
# 回测
# ============================================================
def backtest(data):
    pool_codes = list(data['pool'].keys())
    trade_dates = data['trade_dates']
    season_map = data['season']
    kline = data['kline']
    
    INIT_CAPITAL = 1_000_000
    capital = INIT_CAPITAL
    positions = {}
    trades = []
    
    t_start = time.time()
    
    for i, td in enumerate(trade_dates):
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i+1}/{len(trade_dates)}] {td} | 持仓{len(positions)} | 资金{capital:,.0f} | {elapsed:.0f}s")
            # 定时保存中间结果
            if (i+1) % 300 == 0:
                save_tmp(trades)
        
        season = get_season(td, season_map)
        sp = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
        
        # --- 卖出 ---
        to_sell = []
        for code, pos in positions.items():
            # 获取当前价
            cur_price = None
            if code in kline and td in kline[code]:
                cur_price = kline[code][td]['close']
            else:
                closes = sorted([d for d in kline.get(code,{}).keys() if d <= td])
                if closes:
                    cur_price = kline[code][closes[-1]]['close']
            if not cur_price:
                continue
            
            hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(pos['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cur_price - pos['buy_price']) / pos['buy_price'] * 100
            
            if cur_price > pos.get('high_since_buy', pos['buy_price']):
                pos['high_since_buy'] = cur_price
            
            high = pos.get('high_since_buy', pos['buy_price'])
            trailing_dd = (high - cur_price) / high * 100 if high > 0 else 0
            
            sell_reason = None
            
            if profit_pct <= -sp['t1_stop']:
                sell_reason = f"T1止损{sp['t1_stop']}%"
            elif profit_pct > 0 and trailing_dd >= sp['t2_stop'] and sp['t2_enabled']:
                sell_reason = f"T2回撤{sp['t2_stop']}%"
            elif profit_pct > 0 and trailing_dd >= sp['trailing_stop']:
                sell_reason = f"移动止盈{sp['trailing_stop']}%"
            elif hold_days >= pos.get('max_hold', sp['max_hold']):
                new_score, _ = score_stock_mem(code, td, data)
                if new_score >= sp['p4_threshold'] and sp['p4_extend'] > 0:
                    pos['max_hold'] = hold_days + sp['p4_extend']
                else:
                    sell_reason = f"持有到期{pos.get('max_hold', sp['max_hold'])}日"
            
            if sell_reason:
                to_sell.append((code, cur_price, profit_pct, sell_reason, hold_days, season))
        
        for code, price, pct, reason, hd, sea in to_sell:
            pos = positions.pop(code)
            capital += price * pos['qty']
            trades.append({
                'ts_code': code, 'name': data['pool'].get(code, ''),
                'buy_date': pos['buy_date'], 'sell_date': td,
                'hold_days': hd, 'buy_price': round(pos['buy_price'], 3),
                'sell_price': round(price, 3), 'profit_pct': round(pct, 2),
                'season': sea, 'exit_reason': reason, 'qty': pos['qty'],
            })
        
        # --- 买入 ---
        if len(positions) < 8:
            candidates = []
            for code in pool_codes:
                if code in positions:
                    continue
                score, det = score_stock_mem(code, td, data)
                if score >= sp['buy_line']:
                    candidates.append((code, score))
            
            candidates.sort(key=lambda x: x[1], reverse=True)
            max_buy = 8 - len(positions)
            for code, score in candidates[:max_buy]:
                cur_price = None
                if code in kline and td in kline[code]:
                    cur_price = kline[code][td]['close']
                elif code in kline:
                    cls = sorted([d for d in kline[code].keys() if d <= td])
                    if cls: cur_price = kline[code][cls[-1]]['close']
                if not cur_price or cur_price <= 0:
                    continue
                
                qty = int((capital * 0.12) / cur_price / 100) * 100
                if qty < 100:
                    continue
                cost = qty * cur_price
                if cost > capital:
                    continue
                
                capital -= cost
                positions[code] = {
                    'buy_date': td, 'buy_price': cur_price, 'qty': qty,
                    'season': season, 'high_since_buy': cur_price,
                    'max_hold': sp['max_hold'],
                }
    
    elapsed = time.time() - t_start
    
    # 未平仓按最后交易日价格估值
    last_td = trade_dates[-1]
    for code, pos in list(positions.items()):
        last_price = None
        if code in kline and last_td in kline[code]:
            last_price = kline[code][last_td]['close']
        elif code in kline:
            cls = sorted([d for d in kline[code].keys() if d <= last_td])
            if cls: last_price = kline[code][cls[-1]]['close']
        if last_price:
            capital += last_price * pos['qty']
    
    final_value = capital
    total_return = (final_value - INIT_CAPITAL) / INIT_CAPITAL * 100
    
    # 统计
    wins = [t for t in trades if t['profit_pct'] > 0]
    losses = [t for t in trades if t['profit_pct'] <= 0]
    win_rate = len(wins)/len(trades)*100 if trades else 0
    avg_win = sum(t['profit_pct'] for t in wins)/len(wins) if wins else 0
    avg_loss = sum(t['profit_pct'] for t in losses)/len(losses) if losses else 0
    pf = abs(sum(t['profit_pct'] for t in wins)/sum(t['profit_pct'] for t in losses)) if losses and sum(t['profit_pct'] for t in losses) != 0 else float('inf')
    
    print(f"\n{'='*60}")
    print(f"May & Main 最终方案 全量回测结果")
    print(f"{'='*60}")
    print(f"初始资金: {INIT_CAPITAL:,.0f}")
    print(f"最终市值: {final_value:,.0f}")
    print(f"总收益率: {total_return:+.2f}%")
    print(f"交易笔数: {len(trades)}")
    print(f"胜率: {win_rate:.2f}%")
    print(f"盈利因子: {pf:.2f}")
    print(f"平均盈利: {avg_win:+.2f}%")
    print(f"平均亏损: {avg_loss:+.2f}%")
    print(f"平均持有: {sum(t['hold_days'] for t in trades)/len(trades) if trades else 0:.1f}天")
    print(f"耗时: {elapsed:.0f}s")
    
    # 持有期分布
    by_hold = {'1-5日': [], '6-10日': [], '11-20日': [], '21-30日': [], '31日+': []}
    for t in trades:
        if t['hold_days'] <= 5: by_hold['1-5日'].append(t)
        elif t['hold_days'] <= 10: by_hold['6-10日'].append(t)
        elif t['hold_days'] <= 20: by_hold['11-20日'].append(t)
        elif t['hold_days'] <= 30: by_hold['21-30日'].append(t)
        else: by_hold['31日+'].append(t)
    
    print(f"\n持有期分组:")
    for k, v in by_hold.items():
        if v:
            wr = len([t for t in v if t['profit_pct']>0])/len(v)*100
            avg = sum(t['profit_pct'] for t in v)/len(v)
            print(f"  {k}: {len(v)}笔, 胜率{wr:.1f}%, 均收益{avg:+.2f}%")
    
    # 季节分组
    by_season = defaultdict(list)
    for t in trades:
        by_season[t['season']].append(t)
    print(f"\n季节分组:")
    for k, v in sorted(by_season.items()):
        wr = len([t for t in v if t['profit_pct']>0])/len(v)*100
        avg = sum(t['profit_pct'] for t in v)/len(v)
        print(f"  {k}: {len(v)}笔, 胜率{wr:.1f}%, 均收益{avg:+.2f}%")
    
    # V11对比
    print(f"\n{'='*60}")
    print(f"与V11基准对比 (326只×3.4年)")
    print(f"{'='*60}")
    print(f"          V11基准      新引擎(本次)")
    print(f"总收益率   +5.44%       {total_return:+.2f}%")
    print(f"胜率       37.81%      {win_rate:.2f}%")
    print(f"盈利因子   2.31        {pf:.2f}")
    print(f"交易笔数   -           {len(trades)}")
    
    result = {
        'initial_capital': INIT_CAPITAL,
        'final_value': round(final_value, 2),
        'total_return_pct': round(total_return, 2),
        'trade_count': len(trades),
        'win_rate': round(win_rate, 2),
        'profit_factor': round(pf, 2) if pf != float('inf') else 999,
        'avg_hold_days': round(sum(t['hold_days'] for t in trades)/len(trades), 1) if trades else 0,
        'elapsed_seconds': round(elapsed, 0),
        'trades': trades,
    }
    
    with open('/tmp/backtest_final_v2_result.json', 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到 /tmp/backtest_final_v2_result.json")
    return result


def save_tmp(trades):
    """中途保存"""
    with open('/tmp/backtest_final_v2_tmp.json', 'w') as f:
        json.dump({'trade_count': len(trades), 'note': 'in progress'}, f)
    print(f"    → 中间快照: {len(trades)}笔交易")


if __name__ == '__main__':
    t0 = time.time()
    print("=== May & Main 最终方案全量回测 ===")
    print("阶段1: 预取数据...")
    data = load_all_data()
    print(f"阶段2: 启动回测 ({len(data['trade_dates'])}个交易日, {len(data['pool'])}只股票)")
    result = backtest(data)
    print(f"\n总耗时: {time.time()-t0:.0f}s")
