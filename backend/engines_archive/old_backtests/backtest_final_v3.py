#!/usr/bin/env python3
"""
May & Main 最终方案 全量回测 V3（逐日流式版，低内存）
======================================================
每日从DB批量查询因子，不预取全量数据到内存。

运行: python3 backtest_final_v3.py
"""
import sys, os, json, time, math
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymysql

MYSQL_PWD = 'iXve1rVBXfdA4tL9'

conn = pymysql.connect(
    host='localhost', user='debian-sys-maint', password='iXve1rVBXfdA4tL9',
    database='stock_db_v2', charset='utf8mb4',
    cursorclass=pymysql.cursors.DictCursor
)
cur = conn.cursor()

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


# ============================================================
# 当日评分（用SQL一次查完344只的所有因子）
# ============================================================
def score_day(td, pool_codes, conn, cur):
    """对当天所有不在持仓的股票评分"""
    # 获取当日K线
    cur.execute("""SELECT ts_code, close, vol FROM daily_kline_qfq 
                   WHERE trade_date=%s AND ts_code IN %s""", 
                (td, tuple(pool_codes)))
    klines = {r['ts_code']: {'close': float(r['close']), 'vol': float(r['vol'])} 
              for r in cur.fetchall() if r['close']}
    
    if not klines:
        return {}
    
    codes_t = list(klines.keys())
    
    # 获取趋势分（strategy_signal 只有最近数据，所有用默认50）
    # 获取结构分（同样默认50）
    
    # 获取5日大单净流入
    cur.execute("""SELECT ts_code, 
                   COALESCE(SUM(buy_lg_amount+buy_elg_amount-sell_lg_amount-sell_elg_amount),0) as net
                   FROM moneyflow 
                   WHERE trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
                   AND ts_code IN %s GROUP BY ts_code""",
                (td, td, tuple(codes_t)))
    flows = {r['ts_code']: float(r['net']) for r in cur.fetchall()}
    
    # 获取5日融资买入均值
    cur.execute("""SELECT ts_code, AVG(rzmre) as avg_rz
                   FROM margin_detail 
                   WHERE trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
                   AND ts_code IN %s AND rzmre IS NOT NULL
                   GROUP BY ts_code""",
                (td, td, tuple(codes_t)))
    margins = {}
    for r in cur.fetchall():
        if r['avg_rz']:
            margins[r['ts_code']] = float(r['avg_rz'])
    
    # 获取250日均价
    # 先获取每个股票250个交易日前的数据点
    # 用SQL窗口函数或子查询
    ma250 = {}
    for code in codes_t:
        cur.execute("""SELECT AVG(close) as ma FROM daily_kline_qfq 
                       WHERE ts_code=%s AND trade_date <= %s 
                       AND trade_date >= DATE_SUB(%s, INTERVAL 250 DAY)""",
                    (code, td, td))
        r = cur.fetchone()
        if r and r['ma']:
            ma250[code] = float(r['ma'])
    
    # 获取20日均量
    avg_vol20 = {}
    for code in codes_t:
        cur.execute("""SELECT AVG(vol) as av FROM daily_kline_qfq 
                       WHERE ts_code=%s AND trade_date < %s 
                       AND trade_date >= DATE_SUB(%s, INTERVAL 20 DAY)""",
                    (code, td, td))
        r = cur.fetchone()
        if r and r['av']:
            avg_vol20[code] = float(r['av'])
    
    results = {}
    for code in codes_t:
        k = klines[code]
        close = k['close']
        vol = k['vol']
        
        # 位置因子
        pos_score = 50
        if code in ma250 and ma250[code] > 0:
            dev = (close - ma250[code]) / ma250[code]
            pos_score = (dev + 0.30) / 0.60 * 100
            pos_score = max(0, min(100, pos_score))
        
        # 大单净流入
        mf_score = 50
        if code in flows:
            net = flows[code]
            mf_score = (net + 500) / (50000 + 500) * 100
            mf_score = max(0, min(100, mf_score))
        
        # 融资融券
        margin_score = 50
        if code in margins and margins[code] > 0:
            margin_score = math.log10(max(1, margins[code] / 10000)) / 5 * 100
            margin_score = max(0, min(100, margin_score))
        
        # 换手率/量比
        vr = 1.0
        if code in avg_vol20 and avg_vol20[code] > 0 and vol > 0:
            vr = vol / avg_vol20[code]
        
        # 综合评分（默认趋势分50，结构分50，动量50）
        composite = (50 * 0.30 + pos_score * 0.15 + 50 * 0.10 +
                     50 * 0.25 + mf_score * 0.15 + margin_score * 0.10)
        
        # 换手率过滤
        if vr < 0.3:
            composite *= 0.95
        elif vr > 5.0:
            composite *= 0.90
        elif 0.8 <= vr <= 2.0:
            composite *= 1.02
        
        composite = max(0, min(100, composite))
        
        results[code] = {
            'score': round(composite, 2),
            'close': close,
            'details': {'pos': round(pos_score,1), 'mf': round(mf_score,1), 
                       'margin': round(margin_score,1), 'vr': round(vr,2)}
        }
    
    return results


# ============================================================
# 回测
# ============================================================
def backtest():
    # 获取回测池
    cur.execute("SELECT ts_code, name FROM backtest_pool")
    pool = {r['ts_code']: r.get('name','') for r in cur.fetchall()}
    pool_codes = list(pool.keys())
    print(f"回测池: {len(pool_codes)}只")
    
    # 获取交易日
    cur.execute("""SELECT DISTINCT trade_date FROM daily_kline_qfq 
                   WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12' 
                   ORDER BY trade_date""")
    trade_dates = [r['trade_date'].strftime('%Y-%m-%d') for r in cur.fetchall()]
    print(f"交易日: {len(trade_dates)}个 ({trade_dates[0]} ~ {trade_dates[-1]})")
    
    # 季节
    cur.execute("SELECT trade_date, season FROM season_state ORDER BY trade_date")
    season_map = {r['trade_date'].strftime('%Y-%m-%d'): r['season'] for r in cur.fetchall()}
    
    def get_season_day(td):
        if td in season_map:
            return season_map[td]
        for d in sorted(season_map.keys(), reverse=True):
            if d <= td:
                return season_map[d]
        return 'chaos'
    
    INIT_CAPITAL = 1_000_000
    capital = INIT_CAPITAL
    positions = {}
    trades = []
    
    t_start = time.time()
    
    for i, td in enumerate(trade_dates):
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i+1}/{len(trade_dates)}] {td} | 持仓{len(positions)} | 资金{capital:,.0f} | {elapsed:.0f}s")
            if (i+1) % 300 == 0:
                with open('/tmp/bt_v3_tmp.json', 'w') as f:
                    json.dump({'trade_count': len(trades), 'td': td}, f)
        
        season = get_season_day(td)
        sp = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
        
        # --- 卖出 ---
        to_sell = []
        for code, pos in positions.items():
            cur_price = None
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
            r = cur.fetchone()
            if r and r['close']:
                cur_price = float(r['close'])
            else:
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
                sell_reason = f"持有到期{pos.get('max_hold', sp['max_hold'])}日"
            
            if sell_reason:
                to_sell.append((code, cur_price, profit_pct, sell_reason, hold_days, season))
        
        for code, price, pct, reason, hd, sea in to_sell:
            pos = positions.pop(code)
            capital += price * pos['qty']
            trades.append({
                'ts_code': code, 'name': pool.get(code, ''),
                'buy_date': pos['buy_date'], 'sell_date': td,
                'hold_days': hd, 'buy_price': round(pos['buy_price'], 3),
                'sell_price': round(price, 3), 'profit_pct': round(pct, 2),
                'season': sea, 'exit_reason': reason, 'qty': pos['qty'],
            })
        
        # --- 买入 ---
        if len(positions) < 8:
            scores = score_day(td, pool_codes, conn, cur)
            candidates = [(c, scores[c]) for c in scores 
                         if c not in positions and scores[c]['score'] >= sp['buy_line']]
            candidates.sort(key=lambda x: x[1]['score'], reverse=True)
            
            max_buy = 8 - len(positions)
            for code, s in candidates[:max_buy]:
                price = s['close']
                qty = int((capital * 0.12) / price / 100) * 100
                if qty < 100:
                    continue
                cost = qty * price
                if cost > capital:
                    continue
                capital -= cost
                positions[code] = {
                    'buy_date': td, 'buy_price': price, 'qty': qty,
                    'season': season, 'high_since_buy': price,
                    'max_hold': sp['max_hold'],
                }
    
    elapsed = time.time() - t_start
    
    # 未持仓估值
    last_td = trade_dates[-1]
    for code, pos in list(positions.items()):
        cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, last_td))
        r = cur.fetchone()
        if r and r['close']:
            capital += float(r['close']) * pos['qty']
    
    final_value = capital
    total_return = (final_value - INIT_CAPITAL) / INIT_CAPITAL * 100
    
    wins = [t for t in trades if t['profit_pct'] > 0]
    losses = [t for t in trades if t['profit_pct'] <= 0]
    win_rate = len(wins)/len(trades)*100 if trades else 0
    avg_win = sum(t['profit_pct'] for t in wins)/len(wins) if wins else 0
    avg_loss = sum(t['profit_pct'] for t in losses)/len(losses) if losses else 0
    pf = abs(sum(t['profit_pct'] for t in wins)/sum(t['profit_pct'] for t in losses)) if losses and sum(t['profit_pct'] for t in losses) != 0 else float('inf')
    avg_hold = sum(t['hold_days'] for t in trades)/len(trades) if trades else 0
    
    print(f"\n{'='*60}")
    print(f"May & Main 最终优化方案 全量回测结果")
    print(f"{'='*60}")
    print(f"初始资金: {INIT_CAPITAL:,.0f}")
    print(f"最终市值: {final_value:,.0f}")
    print(f"总收益率: {total_return:+.2f}%")
    print(f"交易笔数: {len(trades)}")
    print(f"胜率: {win_rate:.2f}%")
    print(f"盈利因子: {pf:.2f}")
    print(f"平均持有: {avg_hold:.1f}天")
    print(f"耗时: {elapsed:.0f}s")
    
    # 持有期
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
    
    # 季节
    by_season = defaultdict(list)
    for t in trades:
        by_season[t['season']].append(t)
    print(f"\n季节分组:")
    for k, v in sorted(by_season.items()):
        wr = len([t for t in v if t['profit_pct']>0])/len(v)*100
        avg = sum(t['profit_pct'] for t in v)/len(v)
        print(f"  {k}: {len(v)}笔, 胜率{wr:.1f}%, 均收益{avg:+.2f}%")
    
    result = {
        'initial_capital': INIT_CAPITAL, 'final_value': round(final_value, 2),
        'total_return_pct': round(total_return, 2), 'trade_count': len(trades),
        'win_rate': round(win_rate, 2), 'profit_factor': round(pf, 2) if pf != float('inf') else 999,
        'avg_hold_days': round(avg_hold, 1),
        'elapsed_seconds': round(elapsed, 0), 'trades': trades,
    }
    
    with open('/tmp/backtest_final_v3_result.json', 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结果: /tmp/backtest_final_v3_result.json")
    
    cur.close()
    conn.close()
    return result


if __name__ == '__main__':
    t0 = time.time()
    print("May & Main 最终方案全量回测 V3 (逐日流式)")
    result = backtest()
    print(f"总耗时: {time.time()-t0:.0f}s")
