#!/usr/bin/env python3
"""
V13.1 纯正回测 — 全量843只，用子因子重算V13.1评分+置信度校准
周期: 2024-09-02 ~ 2026-07-04
数据源: backtest_score_daily（子因子）+ season_state（置信度）
"""
import sys, os, json, time, math, pymysql
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

SEASON_PARAMS = {
    'summer':         {'buy':75, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'spring':         {'buy':75, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'weak_spring':    {'buy':75, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'chaos':          {'buy':75, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'chaos_spring':   {'buy':75, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'chaos_autumn':   {'buy':75, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':15},
    'autumn':         {'buy':80, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':20},
    'weak_autumn':    {'buy':75, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'winter':         {'buy':80, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':10},
}

SEASON_ALIAS = {'spring': 'weak_spring'}

def confidence_scale(conf):
    if conf >= 0.7: return 1.0
    elif conf >= 0.5: return 0.875
    elif conf >= 0.3: return 0.625
    else: return 0.50

def calibrate(raw, all_raws, scale):
    if not all_raws: return max(0, min(100, raw))
    ss = sorted(all_raws); n = len(ss)
    targets = {5:int(10*scale),10:int(15*scale),15:int(18*scale),20:int(20*scale),25:int(22*scale),
               30:int(24*scale),35:int(26*scale),40:int(28*scale),45:int(29*scale),50:int(30*scale),
               55:int(32*scale),60:int(34*scale),65:int(36*scale),70:int(38*scale),75:int(40*scale),
               80:int(44*scale),85:int(48*scale),90:int(50*scale),93:int(55*scale),95:int(60*scale),
               97:int(68*scale),99:int(75*scale),100:int(80*scale)}
    cm = {}
    for pct, t in targets.items():
        idx = min(int(n*pct/100), n-1)
        cm[ss[idx]] = t
    cm[ss[0]] = max(0, targets[5]-5)
    cm[ss[-1]] = targets[100]
    sr = sorted(cm.keys())
    if raw <= sr[0]: return float(cm[sr[0]])
    if raw >= sr[-1]: return float(cm[sr[-1]])
    for i in range(len(sr)-1):
        lo, hi = sr[i], sr[i+1]
        if lo <= raw <= hi:
            if hi == lo: return float(cm[lo])
            return round(cm[lo] + (raw-lo)/(hi-lo)*(cm[hi]-cm[lo]), 1)
    return round(raw, 1)


def load_seasons(conn, start_date, end_date):
    c = conn.cursor()
    c.execute("SELECT trade_date, season, confidence FROM season_state WHERE index_code='MARKET' AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (start_date, end_date))
    cache = {}
    for r in c.fetchall():
        cache[str(r['trade_date'])] = {'season': r['season'], 'confidence': float(r['confidence'] or 0.5)}
    c.close()
    return cache

def load_dates(conn, start_date, end_date):
    c = conn.cursor()
    c.execute("SELECT DISTINCT trade_date FROM backtest_score_daily WHERE trade_date>=%s AND trade_date<=%s AND chanlun_trend IS NOT NULL ORDER BY trade_date", (start_date, end_date))
    dates = [str(r['trade_date']) for r in c.fetchall()]
    c.close()
    return dates

def load_day(conn, td):
    c = conn.cursor()
    c.execute("SELECT ts_code, chanlun_trend, structure_score, momentum_score, mf_score, close_price FROM backtest_score_daily WHERE trade_date=%s AND chanlun_trend IS NOT NULL AND close_price>0", (td,))
    scores = {}
    for r in c.fetchall():
        scores[r['ts_code']] = {
            'trend': float(r['chanlun_trend'] or 50),
            'structure': float(r['structure_score'] or 50),
            'momentum': float(r['momentum_score'] or 50),
            'mf': float(r['mf_score'] or 50),
            'close': float(r['close_price'] or 0),
        }
    c.close()
    return scores

def backtest(start_date='2026-01-01', end_date='2026-07-04'):
    print(f"\n🚀 V13.1 纯正回测: {start_date} ~ {end_date}")
    print(f"{'='*55}")

    seasons = load_seasons(conn, start_date, end_date)
    print(f"  ✓ 季节: {len(seasons)}天")

    dates = load_dates(conn, start_date, end_date)
    print(f"  ✓ 交易日: {len(dates)}天")

    # 预加载所有日期的评分（节省逐个查询）
    print("🔄 加载评分...")
    day_cache = {}
    for td in dates:
        day_cache[td] = load_day(conn, td)
    print(f"  ✓ {len(day_cache)}天×{len(next(iter(day_cache.values())))}只/天")

    cash = INIT_CAPITAL
    positions = []
    all_trades = []
    portfolio_values = []
    day_scores_loaded = set()

    t0 = time.time()
    for idx, td in enumerate(dates):
        if td not in seasons:
            continue

        sd = seasons[td]
        cur_season = sd['season']
        confidence = sd['confidence']
        scale = confidence_scale(confidence)

        pk = cur_season if cur_season in SEASON_PARAMS else SEASON_ALIAS.get(cur_season, 'chaos')
        sp = SEASON_PARAMS[pk]
        buy_line = sp['buy']; max_hold = sp['hold']
        max_total = sp['max_total']; max_pos = sp['max_pos']

        # ── 检查持仓 ──
        still_hold = []
        for p in positions:
            cur.execute("SELECT close FROM daily_kline WHERE ts_code=%s AND trade_date=%s LIMIT 1", (p['ts_code'], td))
            r = cur.fetchone()
            if not r:
                still_hold.append(p); continue
            cp = float(r['close'])

            hold_days = (datetime.strptime(td,'%Y-%m-%d') - datetime.strptime(p['buy_date'],'%Y-%m-%d')).days
            profit_pct = (cp - p['buy_price']) / p['buy_price']
            p['peak_price'] = max(p.get('peak_price', p['buy_price']), cp)

            t1 = sp['t1']/100.0; t2 = sp['t2']/100.0; tr = sp['trail']/100.0
            reason = None
            if profit_pct <= -t1: reason = f'止损T1({profit_pct*100:.1f}%)'
            elif hold_days >= 2 and profit_pct <= -t2: reason = f'止损T2({profit_pct*100:.1f}%)'
            elif tr > 0 and p['peak_price'] > p['buy_price']:
                dd = (p['peak_price'] - cp) / p['peak_price']
                if dd >= tr: reason = f'止盈({dd*100:.1f}%)'
            elif hold_days >= max_hold: reason = f'到期({hold_days}d)'

            if reason:
                gross = cp * p['shares']; pnl = gross - p['cost'] - gross * CHARGE_RATE
                cash += gross - gross * CHARGE_RATE
                all_trades.append({**p, 'exit_date': td, 'exit_price': cp, 'hold_days': hold_days,
                                   'profit_pct': round(profit_pct*100,2), 'pnl': round(pnl,2), 'reason': reason})
            else:
                still_hold.append(p)
        positions = still_hold

        # ── 买入 ──
        cur_pos_val = sum(p['cost'] for p in positions)
        max_total_val = INIT_CAPITAL * max_total / 100.0

        if cur_pos_val < max_total_val and td in day_cache:
            ds = day_cache[td]
            # 算V13.1原始分
            raw_list = []
            raw_map = {}
            close_map = {}
            for code, f in ds.items():
                raw = f['trend']*0.40 + f['structure']*0.10 + f['momentum']*0.25 + f['mf']*0.25
                raw_list.append(raw)
                raw_map[code] = raw
                close_map[code] = f['close']

            # 校准
            candidates = [(code, calibrate(raw, raw_list, scale), close_map[code])
                         for code, raw in raw_map.items()]
            candidates = [c for c in candidates if c[1] >= buy_line and c[2] > 0]
            candidates.sort(key=lambda x: x[1], reverse=True)

            for code, cal, cprice in candidates[:BUY_PER_DAY]:
                if any(p['ts_code'] == code for p in positions): continue
                cur_pos_val = sum(p['cost'] for p in positions)
                if cur_pos_val >= max_total_val: break

                avail = cash
                avail_pos = max_total_val - cur_pos_val
                amt = min(INIT_CAPITAL*max_pos/100.0, avail, avail_pos)
                if amt < 10000: continue

                shares = int(amt / cprice / 100) * 100
                if shares < 100: continue
                cost = shares * cprice * (1 + CHARGE_RATE)
                if cost > cash:
                    shares = int(cash*0.98 / cprice / 100) * 100
                    if shares < 100: continue
                    cost = shares * cprice * (1 + CHARGE_RATE)

                cash -= cost
                positions.append({
                    'ts_code': code, 'buy_date': td, 'entry_date': td,
                    'buy_price': cprice, 'entry_price': cprice,
                    'shares': shares, 'cost': cost, 'buy_charge': cprice*shares*CHARGE_RATE,
                    'peak_price': cprice, 'season': cur_season,
                    'calibrated_score': cal,
                })

        # 净值
        pos_mkt = sum(p['buy_price']*p['shares'] for p in positions)
        portfolio_values.append((td, cash + pos_mkt))

        if (idx+1) % 60 == 0:
            print(f"  📅 {td} ({idx+1}/{len(dates)}) | 持仓{len(positions)}只 | ¥{cash/10000:.0f}万 | {len(all_trades)}笔")

    final_pos = sum(p['buy_price']*p['shares'] for p in positions)
    final_val = cash + final_pos
    total_ret = (final_val - INIT_CAPITAL) / INIT_CAPITAL * 100

    peak = INIT_CAPITAL; max_dd = 0
    for _, val in portfolio_values:
        if val > peak: peak = val
        dd = (peak-val)/peak*100
        if dd > max_dd: max_dd = dd

    pt = [t for t in all_trades if t['profit_pct'] > 0]
    lt = [t for t in all_trades if t['profit_pct'] <= 0]
    total_pnl = sum(t['pnl'] for t in all_trades)
    wins = len(pt); losses = len(lt)

    print(f"\n{'='*55}")
    print(f"📊 V13.1 纯正回测 ({start_date} ~ {end_date})")
    print(f"{'='*55}")
    print(f"初始: ¥{INIT_CAPITAL/10000:.0f}万 | 最终: ¥{final_val/10000:.0f}万")
    print(f"收益: {total_ret:+.2f}% | 回撤: {max_dd:.2f}% | 卡玛: {total_ret/max_dd:.2f}" if max_dd else f"收益: {total_ret:+.2f}%")
    print(f"交易: {len(all_trades)}笔 | 胜率: {wins/(wins+losses)*100:.1f}% ({wins}胜/{losses}负)")
    print(f"总盈亏: ¥{total_pnl:.0f} | 均值: ¥{total_pnl/len(all_trades):.0f}" if len(all_trades) else "")
    if wins and losses:
        avg_w = sum(t['pnl'] for t in pt)/wins; avg_l = sum(t['pnl'] for t in lt)/losses
        print(f"盈亏比: {abs(avg_w/avg_l):.2f}" if avg_l else "")
    if len(all_trades):
        print(f"均持有: {sum(t['hold_days'] for t in all_trades)/len(all_trades):.1f}d")
        # 持有期分布
        for lo, hi in [(0,5),(5,10),(10,15),(15,20),(20,30),(30,60),(60,999)]:
            ts = [t for t in all_trades if lo <= t['hold_days'] < hi]
            if ts:
                print(f"  {lo}-{hi}d: {len(ts)}笔 | {sum(1 for t in ts if t['profit_pct']>0)/len(ts)*100:.0f}% | 均{sum(t['profit_pct'] for t in ts)/len(ts):+.2f}%")

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
        backtest('2026-01-01', '2026-07-04')
