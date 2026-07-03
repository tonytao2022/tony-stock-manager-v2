#!/usr/bin/env python3
"""
A/B回测对比 — 验证混沌期强势股回退A轨的效果

A版: 混沌期全走B轨(均值回归) — 改前
B版: 混沌期B轨 + 强势股(A轨≥70)回退A轨 — 改后

对比指标: 总收益率、胜率、盈亏比、最大回撤
"""
import sys, os, json, time, math, pymysql
from datetime import datetime, timedelta
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p6_dual_track_engine import track_momentum, track_reversion, score_stock, MarketContext
from season_engine import SeasonEngine

MYSQL_PWD = 'iXve1rVBXfdA4tL9'
conn = pymysql.connect(host='localhost', user='debian-sys-maint', password=MYSQL_PWD,
                       database='stock_db_v2', charset='utf8mb4',
                       cursorclass=pymysql.cursors.DictCursor, autocommit=True)
cur = conn.cursor()

INIT_CAPITAL = 1_000_000
BUY_PER_DAY = 3
MAX_POS = 8

# 季节参数矩阵（V11固化版）
SEASON_PARAMS = {
    'summer':  {'buy':72, 'hold':60, 't1':12, 't2':9, 'p4':55, 'p4e':15, 'tr':18, 't2on':True, 'maxpos':8},
    'autumn':  {'buy':75, 'hold':25, 't1':8,  't2':6, 'p4':65, 'p4e':5,  'tr':12, 't2on':True, 'maxpos':6},
    'spring':  {'buy':70, 'hold':20, 't1':8,  't2':6, 'p4':60, 'p4e':5,  'tr':12, 't2on':True, 'maxpos':6},
    'winter':  {'buy':85, 'hold':10, 't1':5,  't2':4, 'p4':999,'p4e':0,  'tr':8,  't2on':False,'maxpos':2},
    'chaos':   {'buy':75, 'hold':25, 't1':10, 't2':8, 'p4':65, 'p4e':5,  'tr':12, 't2on':False,'maxpos':6},
    'chaos_spring':{'buy':70, 'hold':20, 't1':8, 't2':6, 'p4':60, 'p4e':5, 'tr':12, 't2on':False,'maxpos':6},
    'chaos_autumn':{'buy':75, 'hold':25, 't1':8, 't2':6, 'p4':65, 'p4e':5, 'tr':12, 't2on':False,'maxpos':4},
}

def get_season(trade_date):
    """获取某天的市场季节"""
    cur.execute("SELECT season FROM season_state WHERE index_code='MARKET' AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1", (trade_date,))
    r = cur.fetchone()
    return r['season'] if r else 'chaos'

def run_backtest(mode='B', dates=None, pool_codes=None):
    """
    mode='A': 混沌期全B轨(改前)
    mode='B': 混沌期B轨+强势股回落A轨(改后)
    """
    cap = INIT_CAPITAL
    pos = {}  # {code: {'q':股数, 'bp':买入价, 'bd':买入日期, 'hp':最高价, 'season':季节}}
    trades = []
    cache = {}
    t0 = time.time()
    
    for di, td in enumerate(dates):
        if (di+1) % 300 == 0:
            el = time.time() - t0
            cap_display = int(cap) + sum(pos[c]['q']*pos[c]['bp'] for c in pos)
            print(f"  [{di+1}/{len(dates)}] {td} 持仓{len(pos)} 总资产{cap_display} {int(el)}s")
        
        # 获取季节
        sea = get_season(td)
        p = SEASON_PARAMS.get(sea, SEASON_PARAMS['chaos'])
        
        # === 卖出 ===
        out = []
        for code, px in pos.items():
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
            kr = cur.fetchone()
            if not kr: continue
            cl = float(kr['close'])
            bp = px['bp']
            hp = max(px['hp'], cl)
            hd = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(px['bd'], '%Y-%m-%d')).days
            pr = (cl - bp) / bp * 100
            
            if pr < -p['t1']:
                out.append((code, cl, pr, 'T1止损'))
            elif p['t2on'] and pr < -p['t2']:
                out.append((code, cl, pr, 'T2回撤'))
            elif hp >= bp * 1.08 and (hp - cl) / hp * 100 > p['tr']:
                out.append((code, cl, pr, '移动止盈'))
            elif hd >= p['hold']:
                if p['p4'] < 999:
                    # P4延持需要高评分
                    if code not in cache:
                        ctx = MarketContext({'trade_date': td, 'season': sea, 'regime': 'range',
                                            'hengjiyuan_level': 'weak_heng', 'scoring_strategy': 'momentum_v2'})
                        r = track_momentum(code, ctx)
                        cache[code] = (r.get('score', 50), r.get('details', {}))
                    sc, _ = cache.get(code, (50, {}))
                    if sc >= p['p4']:
                        continue
                out.append((code, cl, pr, '到期'))
        
        for code, cl, pr, reason in out:
            q = pos[code]['q']
            cap += q * cl
            trades.append({'ts_code':code, 'buy_date':pos[code]['bd'], 'sell_date':td,
                          'hold_days':(datetime.strptime(td,'%Y-%m-%d')-datetime.strptime(pos[code]['bd'],'%Y-%m-%d')).days,
                          'buy_price':pos[code]['bp'], 'sell_price':cl, 'profit_pct':round(pr,2), 'season':sea})
            del pos[code]
        
        # === 买入 ===
        buy_count = min(BUY_PER_DAY, MAX_POS - len(pos))
        if buy_count <= 0:
            continue
        
        candidates = []
        for code in pool_codes:
            if code in pos: continue
            
            ctx = MarketContext({'trade_date': td, 'season': sea, 'regime': 'range',
                                'hengjiyuan_level': 'weak_heng', 'scoring_strategy': 'momentum_v2'})
            
            if mode == 'A':
                # A版: 全B轨（改前）
                r = track_reversion(code, ctx)
                sc = r.get('score', 50)
                track_name = 'reversion'
            else:
                # B版: B轨+强势股回退A轨（改后 = 当前score_stock逻辑）
                r = score_stock(code, ctx)
                sc = r.get('score', 50)
                track_name = r.get('track', 'reversion')
            
            if sc >= p['buy']:
                candidates.append((code, sc, track_name))
        
        candidates.sort(key=lambda x: -x[1])
        for code, sc, tr in candidates[:buy_count]:
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
            kr = cur.fetchone()
            if not kr: continue
            close = float(kr['close'])
            qty = min(int((cap * 0.12) / close), int(cap * 0.12 / max(close, 0.01)))
            if qty <= 0: continue
            cost = qty * close
            if cost > cap: continue
            cap -= cost
            pos[code] = {'q': qty, 'bp': close, 'bd': td, 'hp': close, 'sea': sea}
    
    # 尾盘平仓
    for code, px in pos.items():
        cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, dates[-1]))
        kr = cur.fetchone()
        if kr:
            cl = float(kr['close'])
            cap += px['q'] * cl
            pr = (cl - px['bp']) / px['bp'] * 100
            trades.append({'ts_code':code, 'buy_date':px['bd'], 'sell_date':dates[-1],
                          'hold_days':(datetime.strptime(dates[-1],'%Y-%m-%d')-datetime.strptime(px['bd'],'%Y-%m-%d')).days,
                          'buy_price':px['bp'], 'sell_price':cl, 'profit_pct':round(pr,2), 'season':px['sea']})
    
    total_return = (cap - INIT_CAPITAL) / INIT_CAPITAL * 100
    wins = [t for t in trades if t['profit_pct'] > 0]
    losses = [t for t in trades if t['profit_pct'] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t['profit_pct'] for t in wins) / sum(abs(t['profit_pct']) for t in losses)) if losses else float('inf')
    
    # 最大回撤
    peak = INIT_CAPITAL
    mdd = 0
    day_cap = INIT_CAPITAL
    for td in dates:
        cur_cap = cap
        for code, px in pos.items():
            if code in [t['ts_code'] for t in trades if t['sell_date'] >= td]:
                continue
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
            kr = cur.fetchone()
            if kr:
                cur_cap += px['q'] * float(kr['close'])
        if cur_cap > peak:
            peak = cur_cap
        dd = (peak - cur_cap) / peak * 100
        if dd > mdd: mdd = dd
    
    return {
        'mode': mode,
        'total_return': round(total_return, 2),
        'win_rate': round(win_rate, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'max_drawdown': round(mdd, 2),
        'total_trades': len(trades),
        'total_capital': round(cap, 2),
    }


if __name__ == '__main__':
    # 取回测池
    cur.execute("SELECT ts_code, name FROM backtest_pool")
    pool = {r['ts_code']: r.get('name','') for r in cur.fetchall()}
    pool_codes = list(pool.keys())
    print(f"回测池: {len(pool_codes)}只")
    
    # 取交易日（覆盖有缠论数据的时段）
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date BETWEEN '2024-01-01' AND '2026-06-12' ORDER BY trade_date")
    dates = [r['trade_date'].strftime('%Y-%m-%d') for r in cur.fetchall()]
    print(f"交易日: {len(dates)}个 ({dates[0]} ~ {dates[-1]})")
    
    print("\n" + "="*60)
    print("A/B回测对比: 混沌期B轨改前(A) vs 改后(B)")
    print("="*60)
    
    for mode in ['A', 'B']:
        print(f"\n🔄 运行 {mode}版 回测...")
        result = run_backtest(mode=mode, dates=dates, pool_codes=pool_codes)
        
        print(f"\n📊 {mode}版 回测结果:")
        print(f"  总收益率: {result['total_return']:+.2f}%")
        print(f"  胜率: {result['win_rate']:.1f}%")
        print(f"  平均盈利: {result['avg_win']:+.2f}%")
        print(f"  平均亏损: {result['avg_loss']:.2f}%")
        print(f"  盈亏比: {result['profit_factor']:.2f}")
        print(f"  最大回撤: {result['max_drawdown']:.2f}%")
        print(f"  总交易: {result['total_trades']}笔")
    
    conn.close()
    print("\n✅ 回测完成")
