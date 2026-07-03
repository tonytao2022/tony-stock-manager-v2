#!/usr/bin/env python3
"""
V12.3 全量回测 — 真实引擎(score_stock含强势股回退A轨) + V12.3季节参数矩阵(MAY优化版)
参数基于V12.2回测数据分析：summer买入线68→65、仓位40%→50%；chaos系买入线75→72

回测方式：每天对候选池逐只评分→买入，无简化
评分引擎：p6_dual_track_engine.score_stock (V12.3改版，含_is_strong_stock)
"""
import sys, os, json, time, math, pymysql
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p6_dual_track_engine import score_stock, MarketContext, _is_strong_stock
from season_engine import SeasonEngine
from db_config import _get_db_config

# ── 动态获取数据库连接 ──
_cfg = _get_db_config()
conn = pymysql.connect(host=_cfg['host'], port=_cfg['port'], user=_cfg['user'], password=_cfg['password'],
                       database=_cfg['database'], charset='utf8mb4',
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

INIT_CAPITAL = 1_000_000
POS_LIMIT = 8
BUY_PER_DAY = 3
CHARGE_RATE = 0.0005  # 手续费万分之五

# V12.3 季节参数矩阵（MAY优化版 v2.0）
# MAY建议改动：summer买入线68→65、仓位40%→50%；chaos系买入线75→72；
# spring(仅1笔样本)归入chaos_spring；chaos_autumn归入autumn(合并简化)
V122_PARAMS = {
    'summer':  {'buy':65, 'hold':60, 't1':12, 't2':9,  'p4_min':55, 'p4_ext':15, 'trailing':18, 't2_on':True,  'max_pos_pct':50},
    'autumn':  {'buy':72, 'hold':25, 't1':8,  't2':6,  'p4_min':65, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':15},
    'chaos_spring':{'buy':72, 'hold':20, 't1':8,  't2':6,  'p4_min':65, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':15},
    'chaos':   {'buy':72, 'hold':25, 't1':10, 't2':8,  'p4_min':65, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':15},
    'winter':  {'buy':80, 'hold':10, 't1':5,  't2':4,  'p4_min':999,'p4_ext':0, 'trailing':8,  't2_on':False, 'max_pos_pct':5},
}

def judge_scoring_strategy_for_date(td_str):
    """
    根据历史season_state和regime判断该日期的scoring_strategy
    动量A轨: summer/spring/chaos_spring
    回归B轨: autumn/winter/chaos_autumn
    chaos(中性混沌): 看regime, 中性+非熊→动量, 偏空/中性+熊→回归
    简化：从数据库中已有的season_state读取当时判定
    """
    cur.execute("""
        SELECT s.season, 
               CASE 
                   WHEN s.season IN ('summer','chaos_spring') THEN 'momentum'
                   WHEN s.season IN ('autumn','winter') THEN 'reversion'
                   WHEN s.season = 'chaos' THEN (SELECT scoring_strategy FROM season_state WHERE index_code='MARKET' AND trade_date=%s)
                   ELSE 'momentum'
               END as strategy
        FROM season_state s
        WHERE s.index_code='MARKET' AND s.trade_date=%s
    """, (td_str, td_str))
    r = cur.fetchone()
    if r:
        return r['season'], r['strategy']
    
    # 找不到精确匹配，用最近
    cur.execute("""
        SELECT s.season,
               CASE 
                   WHEN s.season IN ('summer','spring','chaos_spring') THEN 'momentum'
                   WHEN s.season IN ('autumn','winter','chaos_autumn') THEN 'reversion'
                   WHEN s.season = 'chaos' THEN (SELECT scoring_strategy FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1)
                   ELSE 'momentum'
               END as strategy
        FROM season_state s
        WHERE s.index_code='MARKET' AND s.trade_date<=%s
        ORDER BY s.trade_date DESC LIMIT 1
    """, (td_str,))
    r = cur.fetchone()
    if r:
        return r['season'], r['strategy']
    return 'chaos', 'momentum'


def score_for_date(code, td_str, sea, strategy):
    """用V12.3引擎评分（含B轨强势股回退A轨）"""
    ctx = MarketContext({
        'trade_date': td_str,
        'market_season': sea,
        'season': sea,
        'regime': 'range',
        'hengjiyuan_level': 'weak_heng',
        'scoring_strategy': strategy,
        'market_scoring_strategy': strategy,
    })
    r = score_stock(code, ctx)
    return r.get('score', 50), r.get('details', {}), r.get('_bailout', False)


def backtest():
    # 1. 回测池
    cur.execute("SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE'")
    pool = {r['ts_code']: r.get('name','') for r in cur.fetchall()}
    codes = list(pool.keys())
    print("回测池: %d只" % len(codes))
    
    # 2. 交易日历
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date BETWEEN '2023-07-05' AND '2026-06-15' ORDER BY trade_date")
    dates_rs = [r['trade_date'].strftime('%Y-%m-%d') for r in cur.fetchall()]
    print("交易日: %d个 (%s ~ %s)" % (len(dates_rs), dates_rs[0], dates_rs[-1]))
    
    # 3. 预加载季节映射
    cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
    sm = {}
    for r in cur.fetchall():
        sm[r['trade_date'].strftime('%Y-%m-%d')] = r['season']
    print("季节映射: %d天" % len(sm))
    
    def get_season(td):
        if td in sm: return sm[td]
        best = 'chaos'
        for d in sorted(sm.keys(), reverse=True):
            if d <= td: return sm[d]
        return best
    
    cap = INIT_CAPITAL
    pos = {}
    trades = []
    score_cache = {}  # key: (code, date_str) -> score
    season_trades = defaultdict(list)
    t0 = time.time()
    
    for i, td in enumerate(dates_rs):
        if (i+1) % 100 == 0:
            el = time.time() - t0
            print("  [%d/%d] %s | 持仓%d | 资金%.0f | %ds | 累计%d笔交易" % 
                  (i+1, len(dates_rs), td, len(pos), cap, int(el), len(trades)), flush=True)
        
        sea = get_season(td)
        cfg = V122_PARAMS.get(sea, V122_PARAMS['chaos'])
        
        # 判定该日期的scoring_strategy
        _, strategy = judge_scoring_strategy_for_date(td)
        
        # === 卖出 ===
        out = []
        for code, px in list(pos.items()):
            # 取当日收盘价
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
            kr = cur.fetchone()
            if not kr: continue
            cl = float(kr['close'])
            bp = px['buy_price']
            hp = max(px['peak'], cl)
            hd = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(px['buy_date'], '%Y-%m-%d')).days
            ret = (cl - bp) / bp * 100
            
            reason = None
            # T1 硬止损
            if ret < -cfg['t1']:
                reason = 'T1止损'
            # T2 回撤止损
            elif cfg['t2_on'] and ret < -cfg['t2']:
                reason = 'T2回撤'
            # 移动止盈
            elif hp >= bp * 1.08 and (hp - cl) / hp * 100 > cfg['trailing']:
                reason = '移动止盈'
            # 持有到期
            elif hd >= cfg['hold']:
                if cfg['p4_min'] < 999:
                    # P4延持
                    score_key = (code, td)
                    sc = score_cache.get(score_key)
                    if sc is None:
                        sc, _, _ = score_for_date(code, td, sea, strategy)
                        score_cache[score_key] = sc
                    if sc >= cfg['p4_min'] and hd < cfg['hold'] + cfg['p4_ext']:
                        pass  # 延持
                    else:
                        reason = '到期平仓'
                else:
                    reason = '到期平仓'
            
            if reason:
                qty = px['qty']
                charge = cl * qty * CHARGE_RATE
                cap += cl * qty - charge
                t = {'ts_code':code,'name':pool.get(code,''),'buy_date':px['buy_date'],
                     'sell_date':td,'hold_days':hd,'buy_price':bp,
                     'sell_price':cl,'profit_pct':round(ret,2),'season':px['season'],
                     'exit_reason':reason,'qty':qty}
                trades.append(t)
                season_trades[px['season']].append(t)
                del pos[code]
        
        # === 买入 ===
        # 不持仓时或持仓小于上限时再买入
        if len(pos) < POS_LIMIT:
            open_slots = POS_LIMIT - len(pos)
            buy_cnt = min(BUY_PER_DAY, open_slots)
            cand = []
            
            # 不在持仓里的候选code
            need_check = [c for c in codes if c not in pos]
            for code in need_check:
                score_key = (code, td)
                sc = score_cache.get(score_key)
                if sc is None:
                    sc, _, bailout = score_for_date(code, td, sea, strategy)
                    score_cache[score_key] = sc
                if sc >= cfg['buy']:
                    cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
                    kr = cur.fetchone()
                    if kr:
                        cand.append((code, sc, float(kr['close'])))
            
            # 按评分排序
            cand.sort(key=lambda x: -x[1])
            for code, sc, cl in cand[:buy_cnt]:
                if len(pos) >= POS_LIMIT: break
                if code in pos: continue
                # 锥形仓位：评分越高仓位越大
                max_amt = cap * min(cfg['max_pos_pct'] / 100, 0.25)
                trade_amt = min(max_amt, cap / max(1, open_slots))
                qty = max(1, int(trade_amt / cl))
                if qty <= 0: continue
                charge = cl * qty * CHARGE_RATE
                cap -= cl * qty + charge
                pos[code] = {'buy_date':td, 'buy_price':cl, 'qty':qty, 'peak':cl, 'season':sea}
        
        # 周期清理缓存（避免内存膨胀）
        if len(score_cache) > 20000:
            score_cache = {}
    
    # === 最终清算 ===
    fv = cap
    for code, px in list(pos.items()):
        cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, dates_rs[-1]))
        kr = cur.fetchone()
        cl = float(kr['close']) if kr else px['buy_price']
        fv += px['qty'] * cl
        ret = (cl - px['buy_price']) / px['buy_price'] * 100
        t = {'ts_code':code,'name':pool.get(code,''),'buy_date':px['buy_date'],
             'sell_date':dates_rs[-1],'hold_days':999,'buy_price':px['buy_price'],
             'sell_price':cl,'profit_pct':round(ret,2),'season':px['season'],
             'exit_reason':'持仓平仓','qty':px['qty']}
        trades.append(t)
        season_trades[px['season']].append(t)
    
    # === 统计 ===
    total_ret = (fv - INIT_CAPITAL) / INIT_CAPITAL * 100
    win = [t for t in trades if t['profit_pct'] > 0]
    loss = [t for t in trades if t['profit_pct'] <= 0]
    wr = len(win) / len(trades) * 100 if trades else 0
    tw = sum(t['profit_pct'] for t in win)
    tl = abs(sum(t['profit_pct'] for t in loss)) or 1
    pf = tw / tl
    ah = sum(t['hold_days'] for t in trades) / len(trades) if trades else 0
    aw = sum(t['profit_pct'] for t in win) / len(win) if win else 0
    al = sum(t['profit_pct'] for t in loss) / len(loss) if loss else 0
    et = time.time() - t0
    
    print("\n" + "="*65, flush=True)
    print("📊 V12.3 全量回测（MAY优化版 — Summer加仓/Chaos降门槛/合并简化）", flush=True)
    print("="*65, flush=True)
    print("初始资金: %d" % INIT_CAPITAL, flush=True)
    print("最终市值: %.0f" % fv, flush=True)
    print("总收益率: %+.2f%%" % total_ret, flush=True)
    print("交易笔数: %d" % len(trades), flush=True)
    print("胜率: %.2f%%" % wr, flush=True)
    print("盈利因子: %.2f" % pf, flush=True)
    print("平均持有: %.1f日" % ah, flush=True)
    print("均盈利: %+.2f%%" % aw, flush=True)
    print("均亏损: %.2f%%" % al, flush=True)
    print("耗时: %ds" % int(et), flush=True)
    
    # 持有期分组
    bins = [(1,5),(6,10),(11,20),(21,30),(31,60),(61,999)]
    lbs = ['1-5日','6-10日','11-20日','21-30日','31-60日','61日+']
    print("\n--- 持有期分组 ---", flush=True)
    for (lo,hi),lb in zip(bins, lbs):
        sub = [t for t in trades if lo <= t['hold_days'] <= hi]
        if sub:
            w2 = len([t for t in sub if t['profit_pct']>0])/len(sub)*100
            a2 = sum(t['profit_pct'] for t in sub)/len(sub)
            print("  %s: %d笔 胜率%.1f%% 均收益%+.2f%%" % (lb, len(sub), w2, a2), flush=True)
    
    # 季节分组
    print("\n--- 季节分组 ---", flush=True)
    for k in ['summer','chaos_spring','chaos','autumn','winter']:
        v = season_trades.get(k, [])
        if v:
            w2 = len([t for t in v if t['profit_pct']>0])/len(v)*100
            a2 = sum(t['profit_pct'] for t in v)/len(v)
            print("  %s: %d笔 胜率%.1f%% 均收益%+.2f%%" % (k, len(v), w2, a2), flush=True)
    
    # 退出原因
    print("\n--- 退出原因分组 ---", flush=True)
    reasons = defaultdict(list)
    for t in trades: reasons[t['exit_reason']].append(t)
    for rk in ['T1止损','T2回撤','移动止盈','到期平仓','持仓平仓']:
        v = reasons.get(rk, [])
        if v:
            w2 = len([t for t in v if t['profit_pct']>0])/len(v)*100
            a2 = sum(t['profit_pct'] for t in v)/len(v)
            print("  %s: %d笔 胜率%.1f%% 均收益%+.2f%%" % (rk, len(v), w2, a2), flush=True)
    
    print("\n", flush=True)
    print("--- V12.2 vs V12.3 对比 ---", flush=True)
    print("V12.2: +167.31% | 262笔 | 45.04% | PF2.17 | 均持49.3d", flush=True)
    print("V12.3: %+.2f%% | %d笔 | %.2f%% | PF%.2f | 均持%.1fd" % 
          (total_ret, len(trades), wr, pf, ah), flush=True)
    
    res = {'version':'V12.3','initial_capital':INIT_CAPITAL,'final_value':round(fv,2),
           'total_return_pct':round(total_ret,2),'trade_count':len(trades),
           'win_rate':round(wr,2),'profit_factor':round(pf,2),
           'avg_hold_days':round(ah,1),'trades':trades,
           'timestamp':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    out = '/tmp/backtest_v123_result.json'
    with open(out,'w') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out}", flush=True)
    return res

if __name__ == '__main__':
    print("V12.3 全量回测启动...", flush=True)
    t0 = time.time()
    backtest()
    print(f"\n总耗时: {time.time()-t0:.0f}s", flush=True)
