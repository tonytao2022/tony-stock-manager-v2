#!/usr/bin/env python3
"""
全量A/B回测 — 混沌期B轨回退A轨逻辑验证
============================================
A版: 全B轨（改前）
B版: B轨+强势股回退A轨（改后）

数据源: stock_db_v2, 真实K线 + 真实季节 + 真实评分引擎
回测池: watch_pool 310只
时段: 2024-01-01 ~ 2026-06-12 (约600个交易日)
"""
import sys, os, json, time, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = 'iXve1rVBXfdA4tL9'

import pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict

from p6_dual_track_engine import score_stock, track_reversion, MarketContext

# ─── 数据库 ────────────────────────────────────────────────
conn = pymysql.connect(host='localhost', user='debian-sys-maint',
    password='iXve1rVBXfdA4tL9', database='stock_db_v2',
    charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor, autocommit=True)
cur = conn.cursor()

# ─── 参数 ──────────────────────────────────────────────────
INIT_CAP = 1_000_000
MAX_POS = 8
BUY_PER_DAY = 3

SEASON_PARAMS = {
    'summer':  {'buy':72,'hold':60,'t1':12,'t2':9, 'p4':55,'p4e':15,'tr':18,'t2on':True},
    'autumn':  {'buy':75,'hold':25,'t1':8,'t2':6, 'p4':65,'p4e':5,'tr':12,'t2on':True},
    'spring':  {'buy':70,'hold':20,'t1':8,'t2':6, 'p4':60,'p4e':5,'tr':12,'t2on':True},
    'winter':  {'buy':85,'hold':10,'t1':5,'t2':4, 'p4':999,'p4e':0,'tr':8,'t2on':False},
    'chaos':   {'buy':75,'hold':25,'t1':10,'t2':8,'p4':65,'p4e':5,'tr':12,'t2on':False},
    'chaos_spring':{'buy':70,'hold':20,'t1':8,'t2':6,'p4':60,'p4e':5,'tr':12,'t2on':False},
    'chaos_autumn':{'buy':75,'hold':25,'t1':8,'t2':6,'p4':65,'p4e':5,'tr':12,'t2on':False},
}

def get_season(td):
    cur.execute("SELECT season FROM season_state WHERE index_code='MARKET' AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1", (td,))
    r = cur.fetchone()
    return r['season'] if r else 'chaos'

def run_bt(mode, pool_codes, dates):
    """
    mode: 'A'=全B轨, 'B'=score_stock(回退A轨)
    返回: {summary, trades, daily_equity}
    """
    cap = INIT_CAP
    pos = {}
    trades = []
    daily_equity = []
    t0 = time.time()
    
    n = len(dates)
    report_interval = max(1, n // 10)
    
    for di, td in enumerate(dates):
        sea = get_season(td)
        p = SEASON_PARAMS.get(sea, SEASON_PARAMS['chaos'])
        
        # ─── 卖出 ───
        out = []
        for code, px in list(pos.items()):
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code,td))
            kr = cur.fetchone()
            if not kr: continue
            cl = float(kr['close'])
            bp = px['bp']; hp = max(px['hp'], cl)
            hd = (datetime.strptime(td,'%Y-%m-%d') - datetime.strptime(px['bd'],'%Y-%m-%d')).days
            pr = (cl-bp)/bp*100
            
            if pr < -p['t1']:
                out.append((code,cl,pr,'T1止损',hd))
            elif p['t2on'] and pr < -p['t2']:
                out.append((code,cl,pr,'T2回撤',hd))
            elif hp >= bp*1.08 and (hp-cl)/hp*100 > p['tr']:
                out.append((code,cl,pr,'移动止盈',hd))
            elif hd >= p['hold']:
                # P4延持
                if p['p4'] < 999 and hd < p['hold'] + p['p4e']:
                    ctx_t = MarketContext({'trade_date':td,'season':sea,'regime':'range',
                                           'hengjiyuan_level':'weak_heng','scoring_strategy':'momentum_v2'})
                    from p6_dual_track_engine import track_momentum as tm
                    sc = tm(code, ctx_t).get('score',50)
                    if sc >= p['p4']:
                        continue
                out.append((code,cl,pr,'到期',hd))
        
        for code,cl,pr,reason,hd in out:
            cap += pos[code]['q'] * cl
            buy_season = pos[code]['sea']
            # 看buy_season是否带chaos前缀来判断是否是混沌期买入
            trades.append({
                'ts_code':code,
                'buy_date':pos[code]['bd'],
                'sell_date':td,
                'hold_days':hd,
                'buy_price':round(pos[code]['bp'],2),
                'sell_price':round(cl,2),
                'profit_pct':round(pr,2),
                'season':buy_season,
                'sell_reason':reason,
            })
            del pos[code]
        
        # ─── 买入 ───
        buy_n = min(BUY_PER_DAY, MAX_POS - len(pos))
        if buy_n > 0:
            cand = []
            for code in pool_codes:
                if code in pos: continue
                ctx = MarketContext({'trade_date':td,'season':sea,'regime':'range',
                                    'hengjiyuan_level':'weak_heng','scoring_strategy':'momentum_v2'})
                if mode == 'A':
                    sc = track_reversion(code, ctx).get('score',50)
                else:
                    sc = score_stock(code, ctx).get('score',50)
                if sc >= p['buy']:
                    cand.append((code,sc))
            
            cand.sort(key=lambda x:-x[1])
            for code,sc in cand[:buy_n]:
                cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code,td))
                kr = cur.fetchone()
                if not kr: continue
                close = float(kr['close'])
                qty = int((cap*0.12)/max(close,0.01))
                if qty*close > cap or qty <= 0: continue
                cap -= qty*close
                pos[code] = {'q':qty,'bp':close,'bd':td,'hp':close,'sea':sea}
        
        # 每日权益记录
        eq = cap
        for code,px in pos.items():
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code,td))
            kr = cur.fetchone()
            if kr:
                eq += px['q']*float(kr['close'])
        daily_equity.append({'date':td,'equity':round(eq,2)})
        
        if (di+1) % report_interval == 0:
            el = time.time()-t0
            print(f"  [{mode}] [{di+1}/{n}] {td} 持仓{len(pos)} 净值{int(eq)} 耗时{int(el)}s", flush=True)
    
    # 尾盘清仓
    for code,px in list(pos.items()):
        cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code,dates[-1]))
        kr = cur.fetchone()
        if kr:
            cl = float(kr['close'])
            cap += px['q']*cl
            pr = (cl-px['bp'])/px['bp']*100
            trades.append({
                'ts_code':code,'buy_date':px['bd'],'sell_date':dates[-1],
                'hold_days':(datetime.strptime(dates[-1],'%Y-%m-%d')-datetime.strptime(px['bd'],'%Y-%m-%d')).days,
                'buy_price':round(px['bp'],2),'sell_price':round(cl,2),
                'profit_pct':round(pr,2),'season':px['sea'],'sell_reason':'尾盘'
            })
    
    # 计算指标
    ret = (cap-INIT_CAP)/INIT_CAP*100
    wins = [t for t in trades if t['profit_pct']>0]
    losses = [t for t in trades if t['profit_pct']<=0]
    wr = len(wins)/len(trades)*100 if trades else 0
    aw = sum(t['profit_pct'] for t in wins)/len(wins) if wins else 0
    al = sum(t['profit_pct'] for t in losses)/len(losses) if losses else 0
    pf = abs(sum(t['profit_pct'] for t in wins)/sum(abs(t['profit_pct']) for t in losses)) if losses else float('inf')
    
    # 最大回撤
    peak = INIT_CAP; mdd = 0
    for d in daily_equity:
        if d['equity'] > peak: peak = d['equity']
        dd = (peak-d['equity'])/peak*100
        if dd > mdd: mdd = dd
    
    # 胜率按季节
    season_win = defaultdict(lambda:{'w':0,'l':0,'t':[]})
    for t in trades:
        s = t['season']
        if t['profit_pct']>0: season_win[s]['w']+=1
        else: season_win[s]['l']+=1
        season_win[s]['t'].append(t['profit_pct'])
    season_stats = {}
    for s, d in season_win.items():
        total = d['w']+d['l']
        season_stats[s] = {'wins':d['w'],'losses':d['l'],'total':total,
                          'win_rate':round(d['w']/total*100,1) if total>0 else 0,
                          'avg_return':round(sum(d['t'])/len(d['t']),2) if d['t'] else 0}
    
    # 盈亏分布
    profit_dist = {'< -10%':0,'-10%~-5%':0,'-5%~0%':0,'0%~5%':0,'5%~10%':0,'10%~20%':0,'>20%':0}
    for t in trades:
        p = t['profit_pct']
        if p < -10: profit_dist['< -10%']+=1
        elif p < -5: profit_dist['-10%~-5%']+=1
        elif p < 0: profit_dist['-5%~0%']+=1
        elif p < 5: profit_dist['0%~5%']+=1
        elif p < 10: profit_dist['5%~10%']+=1
        elif p < 20: profit_dist['10%~20%']+=1
        else: profit_dist['>20%']+=1
    
    el = time.time()-t0
    return {
        'mode': mode,
        'summary': {
            'total_return': round(ret,2),
            'win_rate': round(wr,1),
            'avg_win': round(aw,2),
            'avg_loss': round(al,2),
            'profit_factor': round(pf,2),
            'max_drawdown': round(mdd,2),
            'total_trades': len(trades),
            'wins': len(wins),
            'losses': len(losses),
            'final_capital': round(cap,2),
            'time_sec': int(el),
        },
        'trades': trades,
        'daily_equity': daily_equity,
        'season_stats': season_stats,
        'profit_dist': profit_dist,
    }

# ─── 主流程 ────────────────────────────────────────────────
print("="*60)
print("全量A/B回测: 混沌期B轨回退A轨逻辑验证")
print("="*60)

cur.execute("SELECT ts_code, name FROM watch_pool")
pool = {r['ts_code']:r.get('name','') for r in cur.fetchall()}
pool_codes = list(pool.keys())
print(f"\n回测池: {len(pool_codes)}只 (watch_pool)")

cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date BETWEEN '2024-01-01' AND '2026-06-12' ORDER BY trade_date")
dates = [r['trade_date'].strftime('%Y-%m-%d') for r in cur.fetchall()]
print(f"交易日: {len(dates)}个 ({dates[0]} ~ {dates[-1]})")

results = {}
for mode in ['A','B']:
    name = 'A(全B轨)' if mode=='A' else 'B(改后)'
    print(f"\n{'='*60}")
    print(f"🔄 {name} 回测开始...")
    print(f"{'='*60}")
    r = run_bt(mode, pool_codes, dates)
    results[mode] = r
    
    s = r['summary']
    print(f"\n📊 {name} 回测结果:")
    print(f"  总收益率: {s['total_return']:+.2f}%")
    print(f"  胜率: {s['win_rate']:.1f}% ({s['wins']}胜/{s['losses']}负)")
    print(f"  平均盈利: {s['avg_win']:+.2f}%")
    print(f"  平均亏损: {s['avg_loss']:.2f}%")
    print(f"  盈亏比: {s['profit_factor']:.2f}")
    print(f"  最大回撤: {s['max_drawdown']:.2f}%")
    print(f"  总交易: {s['total_trades']}笔")
    print(f"  用时: {s['time_sec']}s")

# ─── 对比输出 ──────────────────────────────────────────────
print("\n" + "="*60)
print("A/B 对比汇总")
print("="*60)
ra = results['A']['summary']; rb = results['B']['summary']
metrics = [
    ('总收益率', f"{ra['total_return']:+.2f}%", f"{rb['total_return']:+.2f}%", f"{rb['total_return']-ra['total_return']:+.2f}%"),
    ('胜率', f"{ra['win_rate']:.1f}%", f"{rb['win_rate']:.1f}%", f"{rb['win_rate']-ra['win_rate']:+.1f}%"),
    ('盈亏比', f"{ra['profit_factor']:.2f}", f"{rb['profit_factor']:.2f}", f"{rb['profit_factor']-ra['profit_factor']:+.2f}"),
    ('总交易', str(ra['total_trades']), str(rb['total_trades']), f"+{rb['total_trades']-ra['total_trades']}笔"),
    ('最大回撤', f"{ra['max_drawdown']:.2f}%", f"{rb['max_drawdown']:.2f}%", f"{rb['max_drawdown']-ra['max_drawdown']:+.2f}%"),
    ('最终资产', f"{ra['final_capital']:,.0f}", f"{rb['final_capital']:,.0f}", f"{rb['final_capital']-ra['final_capital']:+,.0f}"),
]
print(f"\n{'指标':<12s} {'A版(全B轨)':<15s} {'B版(改后)':<15s} {'变化':<12s}")
print("-"*54)
for label, a, b, diff in metrics:
    print(f"{label:<12s} {a:<15s} {b:<15s} {diff:<12s}")

# ─── 各季节胜率对比 ──────────────────────────────────────
print(f"\n{'='*60}")
print("各季节胜率对比")
print(f"{'='*60}")
print(f"{'季节':<15s} {'A版胜率':<10s} {'A版交易':<10s} {'B版胜率':<10s} {'B版交易':<10s}")
print("-"*55)
all_seasons = set(list(results['A']['season_stats'].keys()) + list(results['B']['season_stats'].keys()))
for s in sorted(all_seasons):
    a_s = results['A']['season_stats'].get(s,{})
    b_s = results['B']['season_stats'].get(s,{})
    a_wr = f"{a_s.get('win_rate',0):.1f}%" if a_s else '-'
    a_cnt = str(a_s.get('total',0)) if a_s else '-'
    b_wr = f"{b_s.get('win_rate',0):.1f}%" if b_s else '-'
    b_cnt = str(b_s.get('total',0)) if b_s else '-'
    print(f"{s:<15s} {a_wr:<10s} {a_cnt:<10s} {b_wr:<10s} {b_cnt:<10s}")

# ─── 盈亏分布对比 ────────────────────────────────────────
print(f"\n{'='*60}")
print("盈亏分布对比")
print(f"{'='*60}")
bins = ['< -10%','-10%~-5%','-5%~0%','0%~5%','5%~10%','10%~20%','>20%']
print(f"{'区间':<12s} {'A版(笔)':<10s} {'B版(笔)':<10s}")
print("-"*32)
for b in bins:
    print(f"{b:<12s} {results['A']['profit_dist'].get(b,0):<10d} {results['B']['profit_dist'].get(b,0):<10d}")

# ─── 生成JSON结果 ────────────────────────────────────────
output = {
    'summary_a': results['A']['summary'],
    'summary_b': results['B']['summary'],
    'season_stats_a': results['A']['season_stats'],
    'season_stats_b': results['B']['season_stats'],
    'profit_dist_a': results['A']['profit_dist'],
    'profit_dist_b': results['B']['profit_dist'],
    'diff': {
        'total_return_diff': round(rb['total_return']-ra['total_return'],2),
        'win_rate_diff': round(rb['win_rate']-ra['win_rate'],1),
        'profit_factor_diff': round(rb['profit_factor']-ra['profit_factor'],2),
        'trade_count_diff': rb['total_trades']-ra['total_trades'],
        'mdd_diff': round(rb['max_drawdown']-ra['max_drawdown'],2),
    }
}
os.makedirs('/var/www/html/stock-v2/data', exist_ok=True)
with open('/var/www/html/stock-v2/data/backtest_ab_full.json', 'w') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
print(f"\nJSON结果已保存到 /var/www/html/stock-v2/data/backtest_ab_full.json")

# ─── 生成HTML报告 ──────────────────────────────────────
lines = []
lines.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
lines.append('<title>全量A/B回测报告</title>')
lines.append('<style>')
lines.append('body{font-family:-apple-system,Helvetica,"PingFang SC",sans-serif;background:#f5f5f5;margin:0;padding:20px;color:#333}')
lines.append('.container{max-width:1000px;margin:0 auto}')
lines.append('h1{font-size:1.5em;border-bottom:3px solid #2563eb;padding-bottom:10px}')
lines.append('h2{font-size:1.2em;color:#1e40af;margin-top:24px}')
lines.append('.card{background:#fff;border-radius:10px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}')
lines.append('table{width:100%;border-collapse:collapse;font-size:.88em}')
lines.append('th{background:#f8fafc;padding:8px 10px;text-align:left;border-bottom:2px solid #e2e8f0}')
lines.append('td{padding:7px 10px;border-bottom:1px solid #f1f5f9}')
lines.append('.up{color:#16a34a;font-weight:600}.down{color:#dc2626;font-weight:600}')
lines.append('.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.78em}')
lines.append('.tag-green{background:#dcfce7;color:#166534}.tag-red{background:#fee2e2;color:#991b1b}')
lines.append('.bar-bg{background:#e2e8f0;height:20px;border-radius:4px;overflow:hidden;margin-bottom:12px}')
lines.append('.bar-fill{height:100%;display:flex;align-items:center;justify-content:center;font-size:.75em;font-weight:600;color:#fff}')
lines.append('</style></head><body><div class=container>')

lines.append(f'<h1>全量A/B回测报告</h1>')
lines.append(f'<p style="color:#64748b">回测时段: 2024-01-01 ~ 2026-06-12 | 池: watch_pool {len(pool_codes)}只 | 初始资金: {INIT_CAP:,}</p>')

# 核心结论
lines.append('<div class=card>')
lines.append('<h2>核心结论</h2>')
lines.append('<table><tr><th>指标</th><th>A版(改前·全B轨)</th><th>B版(改后·回退A轨)</th><th>变化</th><th>判定</th></tr>')
for label, a, b, diff in metrics[:5]:
    judge = '✅' if ('+' in diff and '胜' not in label and '回撤' not in label) or ('+' not in diff and '回撤' in label) else ('⚠️' if '回撤' in label else '❌')
    cls = 'up' if '+' in diff and '回撤' not in label else ('up' if '-' in diff and '回撤' in label else 'down')
    lines.append(f'<tr><td>{label}</td><td>{a}</td><td class=up>{b}</td><td class={cls}>{diff}</td><td>{judge}</td></tr>')
final_diff = rb['final_capital'] - ra['final_capital']
lines.append(f'<tr><td>最终资产</td><td>{ra["final_capital"]:,.0f}</td><td class=up>{rb["final_capital"]:,.0f}</td><td class={"up" if final_diff>0 else "down"}>{final_diff:+,.0f}</td><td>{"✅" if final_diff>0 else "❌"}</td></tr>')
lines.append('</table></div>')

# 季节胜率
lines.append('<div class=card><h2>各季节胜率对比</h2><table><tr><th>季节</th><th>A版胜率</th><th>A版交易</th><th>B版胜率</th><th>B版交易</th></tr>')
for s in sorted(all_seasons):
    a_s = results['A']['season_stats'].get(s,{})
    b_s = results['B']['season_stats'].get(s,{})
    a_wr = f"{a_s.get('win_rate',0):.1f}%" if a_s else '-'
    a_cnt = str(a_s.get('total',0)) if a_s else '-'
    b_wr = f"{b_s.get('win_rate',0):.1f}%" if b_s else '-'
    b_cnt = str(b_s.get('total',0)) if b_s else '-'
    cls_a = 'up' if a_s and a_s.get('win_rate',0) > 50 else 'down'
    cls_b = 'up' if b_s and b_s.get('win_rate',0) > 50 else 'down'
    lines.append(f'<tr><td>{s}</td><td class={cls_a}>{a_wr}</td><td>{a_cnt}</td><td class={cls_b}>{b_wr}</td><td>{b_cnt}</td></tr>')
lines.append('</table></div>')

# 盈亏分布
lines.append('<div class=card><h2>盈亏分布对比</h2><table><tr><th>亏损区间</th><th>A版</th><th>B版</th><th>盈利区间</th><th>A版</th><th>B版</th></tr>')
bins_l = ['< -10%','-10%~-5%','-5%~0%']
bins_r = ['0%~5%','5%~10%','10%~20%','>20%']
for i in range(max(len(bins_l), len(bins_r))):
    bl = bins_l[i] if i < len(bins_l) else ''
    br = bins_r[i] if i < len(bins_r) else ''
    av = results['A']['profit_dist'].get(bl,0) if bl else ''
    bv = results['B']['profit_dist'].get(bl,0) if bl else ''
    av2 = results['A']['profit_dist'].get(br,0) if br else ''
    bv2 = results['B']['profit_dist'].get(br,0) if br else ''
    lines.append(f'<tr><td>{bl}</td><td class=down>{av}</td><td class=down>{bv}</td><td>{br}</td><td class=up>{av2}</td><td class=up>{bv2}</td></tr>')
lines.append('</table></div>')

# 净值走势简化版（每20日取一点）
lines.append('<div class=card><h2>净值走势对比</h2><table><tr><th>日期</th><th>A版净值</th><th>B版净值</th><th>B-A差值</th><th>可视化</th></tr>')
da = results['A']['daily_equity']
db = results['B']['daily_equity']
step = max(1, len(da)//30)
for i in range(0, len(da), step):
    d = da[i]
    d2 = db[i] if i < len(db) else d
    eq_a = d['equity']; eq_b = d2['equity']
    diff_a = (eq_a/INIT_CAP-1)*100
    diff_b = (eq_b/INIT_CAP-1)*100
    cls_a = 'up' if diff_a >= 0 else 'down'
    cls_b = 'up' if diff_b >= 0 else 'down'
    bar = '<div class=bar-bg style="height:16px">'
    if diff_b >= 0:
        w = min(diff_b/30*100, 100)
        bar += f'<div class=bar-fill style="width:{w}%;background:#16a34a">{diff_b:.1f}%</div>'
    else:
        w = min(abs(diff_b)/30*100, 100)
        bar += f'<div class=bar-fill style="width:{w}%;background:#dc2626">{diff_b:.1f}%</div>'
    bar += '</div>'
    diff_val = diff_b-diff_a
    lines.append(f'<tr><td>{d["date"]}</td><td class={cls_a}>{diff_a:+.2f}%</td><td class={cls_b}>{diff_b:+.2f}%</td><td class={"up" if diff_val>0 else "down"}>{diff_val:+.2f}%</td><td style="min-width:120px">{bar}</td></tr>')
lines.append('</table></div>')

lines.append(f'<div style="text-align:center;color:#94a3b8;font-size:.8em;margin-top:30px">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | V2 Dual-Track</div>')
lines.append('</div></body></html>')

with open('/var/www/html/stock-v2/backtest_ab_full.html','w') as f:
    f.write('\n'.join(lines))

print(f"\n✅ 报告已生成: http://43.128.119.244/stock-v2/backtest_ab_full.html")
conn.close()
PYEOF
