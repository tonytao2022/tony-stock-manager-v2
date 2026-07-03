#!/usr/bin/env python3
"""V11全量回测 — 直接调用P6双轨引擎track_momentum评分"""
import sys, os, json, time, math, pymysql
from datetime import datetime, timedelta
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p6_dual_track_engine import track_momentum, MarketContext
from season_engine import SeasonEngine

MYSQL_PWD = 'iXve1rVBXfdA4tL9'
conn = pymysql.connect(host='localhost', user='debian-sys-maint', password=MYSQL_PWD,
                       database='stock_db_v2', charset='utf8mb4',
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

INIT_CAPITAL = 1_000_000
POS_LIMIT = 8
BUY_PER_DAY = 3

def score_for_date(code, td):
    """用P6双轨引擎评分"""
    ctx = MarketContext({
        'trade_date': td,
        'season': 'chaos',
        'regime': 'range',
        'hengjiyuan_level': 'weak_heng',
        'scoring_strategy': 'momentum_v2',
    })
    r = track_momentum(code, ctx)
    return r.get('score', 50), r.get('details', {})

V11_SEASON = {
    'summer':  {'b':72,'h':60,'t1':12,'t2':9,'p4':55,'p4e':15,'tr':18,'t2on':True},
    'autumn':  {'b':75,'h':25,'t1':8, 't2':6,'p4':65,'p4e':5,'tr':12,'t2on':True},
    'spring':  {'b':70,'h':20,'t1':8, 't2':6,'p4':60,'p4e':5,'tr':12,'t2on':True},
    'chaos_spring':{'b':75,'h':25,'t1':10,'t2':8,'p4':65,'p4e':5,'tr':12,'t2on':False},
    'chaos':   {'b':75,'h':25,'t1':10,'t2':8,'p4':65,'p4e':5,'tr':12,'t2on':False},
    'chaos_autumn':{'b':75,'h':25,'t1':10,'t2':8,'p4':65,'p4e':5,'tr':12,'t2on':False},
    'winter':  {'b':85,'h':10,'t1':5, 't2':4,'p4':999,'p4e':0,'tr':8,'t2on':False},
}

def backtest():
    cur.execute("SELECT ts_code, name FROM backtest_pool")
    pool = {r['ts_code']: r.get('name','') for r in cur.fetchall()}
    pool_codes = list(pool.keys())
    print("回测池: %d只" % len(pool_codes))
    
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12' ORDER BY trade_date")
    dates = [r['trade_date'].strftime('%Y-%m-%d') for r in cur.fetchall()]
    print("交易日: %d个 (%s ~ %s)" % (len(dates), dates[0], dates[-1]))
    
    cur.execute("SELECT trade_date, season FROM season_state ORDER BY trade_date")
    sm = {r['trade_date'].strftime('%Y-%m-%d'): r['season'] for r in cur.fetchall()}
    def gs(td):
        if td in sm: return sm[td]
        for d in sorted(sm.keys(), reverse=True):
            if d <= td: return sm[d]
        return 'chaos'
    
    cap = INIT_CAPITAL
    pos = {}
    trades = []
    cache = {}
    t0 = time.time()
    
    for i, td in enumerate(dates):
        if (i+1) % 200 == 0:
            el = time.time() - t0
            print("  [%d/%d] %s | 持仓%d | 资金%d | %ds" % (i+1, len(dates), td, len(pos), int(cap), int(el)))
        
        sea = gs(td)
        p = V11_SEASON.get(sea, V11_SEASON['chaos'])
        
        # 卖出
        out = []
        for code, px in pos.items():
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
            kr = cur.fetchone()
            if not kr: continue
            cl = float(kr['close'])
            bd = px['bd']
            bp = px['bp']
            hp = max(px['hp'], cl)
            hd = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(bd, '%Y-%m-%d')).days
            pr = (cl - bp) / bp * 100
            
            if pr < -p['t1']:
                out.append((code, cl, pr, 'T1止损%d%%' % p['t1'], hd)); continue
            if p['t2on'] and pr < -p['t2']:
                out.append((code, cl, pr, 'T2回撤%d%%' % p['t2'], hd)); continue
            if hp >= bp * 1.08 and (hp - cl) / hp * 100 > p['tr']:
                out.append((code, cl, pr, '移动止盈%d%%' % p['tr'], hd)); continue
            if hd >= p['h']:
                if p['p4'] < 999:
                    if code not in cache:
                        sc, _ = score_for_date(code, td)
                        cache[code] = sc
                    if cache.get(code, 0) >= p['p4']:
                        continue
                out.append((code, cl, pr, '到期%d日' % p['h'], hd))
        
        for code, cl, pr, reason, hd in out:
            q = pos[code]['q']
            cap += q * cl
            trades.append({'ts_code':code,'name':pool.get(code,''),'buy_date':pos[code]['bd'],
                          'sell_date':td,'hold_days':hd,'buy_price':pos[code]['bp'],
                          'sell_price':cl,'profit_pct':round(pr,2),'season':pos[code]['sea'],
                          'exit_reason':reason,'qty':q})
            del pos[code]
        
        # 买入
        if len(pos) < POS_LIMIT:
            cand = []
            need_score = [c for c in pool_codes if c not in pos]
            for code in need_score:
                if code in cache:
                    sc = cache[code]
                else:
                    sc, _ = score_for_date(code, td)
                    cache[code] = sc
                if sc >= p['b']:
                    cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
                    kr = cur.fetchone()
                    if kr:
                        cand.append((code, sc, float(kr['close'])))
            
            cand.sort(key=lambda x: -x[1])
            for code, sc, cl in cand[:BUY_PER_DAY]:
                if len(pos) >= POS_LIMIT: break
                if code in pos: continue
                trade_amt = min(cap * 0.15, cap / (POS_LIMIT - len(pos)))
                q = max(1, int(trade_amt / cl))
                if q <= 0: continue
                cap -= q * cl
                pos[code] = {'bd':td, 'bp':cl, 'q':q, 'sea':sea, 'hp':cl}
                if code in cache: del cache[code]
        
        if len(cache) > 10000: cache.clear()
    
    # 清仓
    fv = cap
    for code, px in pos.items():
        cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, dates[-1]))
        kr = cur.fetchone()
        cl = float(kr['close']) if kr else px['bp']
        fv += px['q'] * cl
        pr = (cl - px['bp']) / px['bp'] * 100
        trades.append({'ts_code':code,'name':pool.get(code,''),'buy_date':px['bd'],
                      'sell_date':dates[-1],'hold_days':999,'buy_price':px['bp'],
                      'sell_price':cl,'profit_pct':round(pr,2),'season':px['sea'],
                      'exit_reason':'平仓','qty':px['q']})
    
    tr = (fv - INIT_CAPITAL) / INIT_CAPITAL * 100
    w = len([t for t in trades if t['profit_pct'] > 0])
    l = len([t for t in trades if t['profit_pct'] <= 0])
    wr = w / len(trades) * 100 if trades else 0
    tw = sum(t['profit_pct'] for t in trades if t['profit_pct'] > 0)
    tl = abs(sum(t['profit_pct'] for t in trades if t['profit_pct'] <= 0))
    pf = tw / tl if tl > 0 else 999
    ah = sum(t['hold_days'] for t in trades) / len(trades) if trades else 0
    el = time.time() - t0
    
    print("\n" + "="*60)
    print("V11全量回测 (P6双轨引擎)")
    print("="*60)
    print("初始资金: %d" % INIT_CAPITAL)
    print("最终市值: %d" % int(fv))
    print("总收益率: %.2f%%" % tr)
    print("交易笔数: %d" % len(trades))
    print("胜率: %.2f%%" % wr)
    print("盈利因子: %.2f" % pf)
    print("平均持有: %.1f日" % ah)
    print("耗时: %ds" % int(el))
    
    bins = [(1,5),(6,10),(11,20),(21,30),(31,999)]
    lbs = ['1-5日','6-10日','11-20日','21-30日','31日+']
    print("\n持有期分组:")
    for (lo,hi),lb in zip(bins, lbs):
        sub = [t for t in trades if lo <= t['hold_days'] <= hi]
        if sub:
            w2 = len([t for t in sub if t['profit_pct']>0])/len(sub)*100
            a2 = sum(t['profit_pct'] for t in sub)/len(sub)
            print("  %s: %d笔, 胜率%.1f%%, 均收益%+.2f%%" % (lb, len(sub), w2, a2))
    
    bs = defaultdict(list)
    for t in trades: bs[t['season']].append(t)
    print("\n季节分组:")
    for k in sorted(bs.keys()):
        v = bs[k]
        w2 = len([t for t in v if t['profit_pct']>0])/len(v)*100
        a2 = sum(t['profit_pct'] for t in v)/len(v)
        print("  %s: %d笔, 胜率%.1f%%, 均收益%+.2f%%" % (k, len(v), w2, a2))
    
    res = {'initial_capital':INIT_CAPITAL,'final_value':round(fv,2),
           'total_return_pct':round(tr,2),'trade_count':len(trades),
           'win_rate':round(wr,2),'profit_factor':round(pf,2),
           'avg_hold_days':round(ah,1),'trades':trades}
    with open('/tmp/backtest_v11_v2engine.json','w') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("\n结果: /tmp/backtest_v11_v2engine.json")
    return res

if __name__ == '__main__':
    t0 = time.time()
    print("V11全量回测 (P6双轨引擎评分)")
    backtest()
    print("总耗时: %ds" % (time.time()-t0))
