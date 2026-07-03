#!/usr/bin/env python3
"""
P6 V7 全量回测（季节敏感参数矩阵 + 加长持股 + 分档策略）
===========================================================
参数随季节动态调整（Summer/ Autumn/ Winter/ Chaos/ Spring）

夏季：买入线65, 持有45天, 评分>55延10天, T1-10%, T2-7%
秋季：买入线75, 持有20天, 仅T1, -7%/-5%
混沌：买入线75仅T1, 持有15天, 仅T1-8%
冬季：空仓/买入线85
"""
import sys, os, json, time, math
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p6_dual_track_engine import score_stock, MarketContext
from season_engine import SeasonEngine
from db_config import get_connection
import pymysql

# ─── 全局参数 ───
POOL_MONEY = 1_000_000
P1_GRACE = 2

# ─── 季节参数矩阵 ───
SEASON_PARAMS = {
    'summer': {
        'buy_threshold': 65,      # 买入线
        'tier1_min': 75,          # T1分档
        'max_positions': 8,       # 最多同时持仓
        'sl_t1': 0.90,            # T1止损价比例 (buy_price * 0.90 = -10%)
        'sl_t2': 0.93,            # T2止损价比例 (buy_price * 0.93 = -7%)
        'max_hold': 45,           # 最大持有天数
        'p4_extend_score': 55,    # 延期所需最低评分
        'p4_extend_days': 10,     # 延期天数
        'ts_pct': 12,             # 移动止盈回撤%
        't2_enabled': True,       # T2中分段是否可买入
    },
    'autumn': {
        'buy_threshold': 75,
        'tier1_min': 78,
        'max_positions': 6,
        'sl_t1': 0.93,            # -7%
        'sl_t2': 0.95,            # -5%
        'max_hold': 20,
        'p4_extend_score': 999,   # 不延期
        'p4_extend_days': 0,
        'ts_pct': 10,
        't2_enabled': False,      # 仅T1
    },
    'chaos': {
        'buy_threshold': 75,
        'tier1_min': 78,
        'max_positions': 4,
        'sl_t1': 0.92,            # -8%
        'sl_t2': 0.94,            # -6%
        'max_hold': 15,
        'p4_extend_score': 999,
        'p4_extend_days': 0,
        'ts_pct': 10,
        't2_enabled': False,      # 仅T1
    },
    'spring': {
        # 春使用相同的保守参数
        'buy_threshold': 75,
        'tier1_min': 78,
        'max_positions': 4,
        'sl_t1': 0.92,
        'sl_t2': 0.94,
        'max_hold': 15,
        'p4_extend_score': 999,
        'p4_extend_days': 0,
        'ts_pct': 10,
        't2_enabled': False,
    },
    'winter': {
        'buy_threshold': 85,       # 基本买不到
        'tier1_min': 85,
        'max_positions': 0,
        'sl_t1': 0.95,
        'sl_t2': 0.95,
        'max_hold': 10,
        'p4_extend_score': 999,
        'p4_extend_days': 0,
        'ts_pct': 8,
        't2_enabled': False,
    },
}

START_DATE = '2023-01-03'
END_DATE = '2026-06-09'

class Position:
    __slots__ = ('ts_code','buy_date','buy_price','score','hold_days',
                 'current_price','highest_price','exit_reason','sell_date',
                 'warning_day','buy_tier','hold_limit_extended',
                 'extended_hold_limit','entry_season')
    def __init__(self, ts_code, buy_date, buy_price, score, buy_tier, entry_season):
        self.ts_code = ts_code
        self.buy_date = buy_date
        self.buy_price = buy_price
        self.score = score
        self.buy_tier = buy_tier
        self.entry_season = entry_season
        self.hold_days = 0
        self.current_price = buy_price
        self.highest_price = buy_price
        self.exit_reason = None
        self.sell_date = None
        self.warning_day = 0
        self.hold_limit_extended = False
        self.extended_hold_limit = 0

def get_sp(season):
    """获取季节参数"""
    return SEASON_PARAMS.get(season, SEASON_PARAMS['summer'])

def main():
    print("=" * 70)
    print("P6 V7 全量回测（季节敏感参数矩阵 + 加长持股）")
    print("=" * 70)
    t0 = time.time()
    
    # 1. 交易日 + 回测池
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
                (START_DATE, END_DATE))
    trade_dates = [str(r['trade_date']) for r in cur.fetchall()]
    
    cur.execute("SELECT ts_code FROM backtest_pool WHERE `status`='ACTIVE' AND market!='指数'")
    backtest_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    print(f"📋 {len(backtest_codes)}只股票 × {len(trade_dates)}天")
    
    # 2. 收盘价缓存
    cur = conn.cursor(pymysql.cursors.DictCursor)
    close_cache = {}
    for code in backtest_codes:
        cur.execute("SELECT trade_date, close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
                    (code, START_DATE, END_DATE))
        for r in cur.fetchall():
            td = str(r['trade_date'])
            if td not in close_cache:
                close_cache[td] = {}
            close_cache[td][code] = float(r['close'])
    cur.close(); conn.close()
    print(f"  收盘价缓存: {sum(len(v) for v in close_cache.values())}条")
    
    # 3. 预计算季节判定
    se = SeasonEngine()
    season_cache = {}
    print(f"  预计算季节判定...", end='', flush=True)
    for td in trade_dates:
        try:
            td_date = date.fromisoformat(td)
            judge = se.judge_market_season(td_date)
            season_cache[td] = MarketContext(judge)
        except:
            pass
    se.close()
    print(f" {len(season_cache)}天")
    
    # 统计季节分布
    season_dist = defaultdict(int)
    for td, ctx in season_cache.items():
        season_dist[ctx.season] += 1
    print(f"  季节分布: {dict(season_dist)}")
    
    # 4. 回测主循环
    positions = []
    cool_until = {}
    recorder = []
    cash = POOL_MONEY
    total_checks = len(trade_dates)
    
    for idx, td in enumerate(trade_dates):
        ctx = season_cache.get(td)
        if ctx is None:
            continue
        
        season = ctx.season
        sp = get_sp(season)
        close_td = close_cache.get(td, {})
        
        if (idx+1) % 50 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (idx+1) * (total_checks - idx - 1)
            print(f"\r  [{idx+1}/{total_checks}] {season:>6} 持仓{len(positions)}只 "
                  f"交易{len(recorder)}笔 ETA {eta:.0f}s", end='', flush=True)
        
        # ── 检视持仓 ──
        for pos in list(positions):
            pos.hold_days += 1
            close = close_td.get(pos.ts_code)
            if close is None:
                continue
            pos.current_price = close
            
            # 获取当日评分（用于退坡判断）
            try:
                sc = score_stock(pos.ts_code, ctx)
                score_now = sc['score']
            except:
                score_now = None
            
            # 分档止损（使用买入当季的参数）
            entry_sp = get_sp(pos.entry_season)
            if pos.buy_tier == 1:
                stop_price = pos.buy_price * entry_sp['sl_t1']
                sl_label = int((1 - entry_sp['sl_t1']) * 100)
            else:
                stop_price = pos.buy_price * entry_sp['sl_t2']
                sl_label = int((1 - entry_sp['sl_t2']) * 100)
            
            if close <= stop_price:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                    'sell_date':td,'hold_days':pos.hold_days,'season':pos.entry_season,
                    'return_pct':round(ret,2),'exit_reason':f'SL_T{pos.buy_tier}_{sl_label}p'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
                continue
            
            # 移动止盈
            if close > pos.highest_price:
                pos.highest_price = close
            if close <= pos.highest_price * (1 - entry_sp['ts_pct']/100) and pos.hold_days >= 3:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                    'sell_date':td,'hold_days':pos.hold_days,'season':pos.entry_season,
                    'return_pct':round(ret,2),'exit_reason':f'TS_WIN_{ret:.1f}p'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
                continue
            
            # 强制平仓（含延期逻辑）
            max_hd = entry_sp['max_hold']
            if pos.hold_days >= max_hd:
                extend = False
                if entry_sp['p4_extend_days'] > 0 and score_now is not None and score_now >= entry_sp['p4_extend_score']:
                    # 满足延期条件
                    if not pos.hold_limit_extended:
                        pos.hold_limit_extended = True
                        pos.extended_hold_limit = max_hd + entry_sp['p4_extend_days']
                        extend = True
                
                if not extend:
                    ret = (close - pos.buy_price) / pos.buy_price * 100
                    recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                        'sell_date':td,'hold_days':pos.hold_days,'season':pos.entry_season,
                        'return_pct':round(ret,2),'exit_reason':'P4平仓'})
                    cash += close
                    positions.remove(pos)
                    cool_until[pos.ts_code] = td
                    continue
            
            # 已延期后的检查
            if pos.hold_limit_extended and pos.hold_days >= pos.extended_hold_limit:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                    'sell_date':td,'hold_days':pos.hold_days,'season':pos.entry_season,
                    'return_pct':round(ret,2),'exit_reason':'P4延期平仓'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
                continue
            
            # 双层退坡（分档差异化）
            if pos.buy_tier == 1:
                # T1: 3天观察，跌破55退
                if score_now is not None and score_now < 60:
                    pos.warning_day += 1
                    if pos.warning_day >= 3 and score_now < 55:
                        ret = (close - pos.buy_price) / pos.buy_price * 100
                        recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                            'sell_date':td,'hold_days':pos.hold_days,'season':pos.entry_season,
                            'return_pct':round(ret,2),'exit_reason':f'T1退坡跌破55({score_now})'})
                        cash += close
                        positions.remove(pos)
                        cool_until[pos.ts_code] = td
                else:
                    pos.warning_day = 0
            else:
                # T2: 2天观察，跌破60退
                if score_now is not None and score_now < 60:
                    pos.warning_day += 1
                    if pos.warning_day >= 2:
                        ret = (close - pos.buy_price) / pos.buy_price * 100
                        recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                            'sell_date':td,'hold_days':pos.hold_days,'season':pos.entry_season,
                            'return_pct':round(ret,2),'exit_reason':f'T2退坡跌破60({score_now})'})
                        cash += close
                        positions.remove(pos)
                        cool_until[pos.ts_code] = td
                else:
                    pos.warning_day = 0
        
        # ── 买入 ──
        bt = sp['buy_threshold']
        t1_min = sp['tier1_min']
        t2_enabled = sp['t2_enabled']
        
        if len(positions) < sp['max_positions']:
            candidates = []
            for code in backtest_codes:
                if code in cool_until and cool_until[code] >= td:
                    continue
                if any(p.ts_code == code for p in positions):
                    continue
                try:
                    sc = score_stock(code, ctx)
                    s = sc['score']
                    if s >= bt:
                        # 判断T1/T2
                        tier = 1 if s >= t1_min else 2
                        if tier == 2 and not t2_enabled:
                            continue
                        close = close_td.get(code)
                        if close:
                            candidates.append((code, s, close, tier))
                except:
                    pass
            
            candidates.sort(key=lambda x: x[1], reverse=True)
            for code, score_val, close, tier in candidates[:sp['max_positions'] - len(positions)]:
                if any(p.ts_code == code for p in positions):
                    continue
                pos = Position(code, td, close, score_val, tier, season)
                positions.append(pos)
                cash -= close
    
    # 期末平仓
    last_td = trade_dates[-1]
    for pos in positions:
        close = close_cache.get(last_td, {}).get(pos.ts_code, pos.current_price)
        ret = (close - pos.buy_price) / pos.buy_price * 100
        recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
            'sell_date':last_td,'hold_days':pos.hold_days,
            'season':pos.entry_season,
            'return_pct':round(ret,2),'exit_reason':'期末平仓'})
        cash += close
    
    elapsed = time.time() - t0
    print(f"\r  [{total_checks}/{total_checks}] 交易{len(recorder)}笔  ✅")
    
    # ─── 统计 ───
    trades = recorder
    win = [t for t in trades if t['return_pct'] > 0]
    lose = [t for t in trades if t['return_pct'] <= 0]
    
    total_return = (cash - POOL_MONEY) / POOL_MONEY * 100
    years = len(trade_dates) / 244
    
    # 持仓区间
    buckets = [(1,5,'1-5日'),(6,10,'6-10日'),(11,15,'11-15日'),
               (16,20,'16-20日'),(21,30,'21-30日'),(31,45,'31-45日')]
    hold_stats = {}
    for lo, hi, key in buckets:
        items = [t for t in trades if lo <= t['hold_days'] <= hi]
        if items:
            hold_stats[key] = {
                'count': len(items),
                'avg_return': round(sum(t['return_pct'] for t in items)/len(items), 2),
                'win_rate': round(len([t for t in items if t['return_pct']>0])/len(items)*100, 1),
            }
    
    # 按季节统计
    season_stats = defaultdict(lambda: {'count':0,'sum_ret':0,'wins':0})
    for t in trades:
        s = t['season']
        season_stats[s]['count'] += 1
        season_stats[s]['sum_ret'] += t['return_pct']
        if t['return_pct'] > 0:
            season_stats[s]['wins'] += 1
    
    # 退出原因
    exit_grp = defaultdict(lambda: {'count':0,'sum_ret':0})
    for t in trades:
        cat = t['exit_reason'].split('(')[0]
        exit_grp[cat]['count'] += 1
        exit_grp[cat]['sum_ret'] += t['return_pct']
    
    avg_hd = sum(t['hold_days'] for t in trades)/len(trades) if trades else 0
    avg_win = sum(t['return_pct'] for t in win)/len(win) if win else 0
    avg_lose = sum(t['return_pct'] for t in lose)/len(lose) if lose else 0
    pf = abs(sum(t['return_pct'] for t in win)/sum(t['return_pct'] for t in lose)) if lose else 999
    returns = [t['return_pct'] for t in trades]
    avg_r = sum(returns)/len(returns) if returns else 0
    std_r = math.sqrt(sum((r-avg_r)**2 for r in returns)/len(returns)) if len(returns) > 1 else 1
    sharpe = (avg_r/std_r)*math.sqrt(244) if std_r > 0 else 0
    
    result = {
        'strategy': 'V7_季节矩阵_加长持股_买入线夏65秋75_持有夏45秋20_P4延期55+10天',
        'period': f'{START_DATE}~{END_DATE}',
        'years': round(years, 2),
        'start_cap': POOL_MONEY, 'end_cap': round(cash, 2),
        'return_pct': round(total_return, 2),
        'annual_return_pct': round(total_return/years, 2) if years else 0,
        'trades': len(trades),
        'win_rate': round(len(win)/len(trades)*100, 2) if trades else 0,
        'avg_hd': round(avg_hd, 1),
        'avg_win_pct': round(avg_win, 2),
        'avg_lose_pct': round(avg_lose, 2),
        'profit_factor': round(pf, 2),
        'sharpe': round(sharpe, 2),
        'hold_stats': hold_stats,
        'season_dist': {k: round(v['sum_ret']/v['count'],2) for k,v in season_stats.items()},
        'season_trades': {k: {'count':v['count'],'avg_ret':round(v['sum_ret']/v['count'],2),
                              'win_rate':round(v['wins']/v['count']*100,1)} 
                          for k,v in sorted(season_stats.items())},
    }
    
    print(f"\n{'='*70}")
    print(f"📊 V7 全量回测结果")
    print(f"{'='*70}")
    print(f"总收益率: {total_return:.2f}%")
    print(f"年化收益: {result['annual_return_pct']:.2f}%")
    print(f"交易笔数: {len(trades)}笔")
    print(f"胜率: {result['win_rate']}%")
    print(f"均盈: {avg_win:.2f}% | 均亏: {avg_lose:.2f}%")
    print(f"盈利因子: {pf:.2f}")
    print(f"夏普: {sharpe:.2f}")
    print(f"平均持仓: {avg_hd:.1f}天")
    print(f"\n按季节:")
    for s,info in sorted(result['season_trades'].items()):
        print(f"  {s}: {info['count']}笔 均收{info['avg_ret']}% 胜率{info['win_rate']}%")
    print(f"\n持仓区间:")
    for k,v in sorted(hold_stats.items(), key=lambda x: int(x[0].split('日')[0].split('-')[0])):
        print(f"  {k}: {v['count']}笔 均收益{v['avg_return']}% 胜率{v['win_rate']}%")
    print(f"\n与V5(V6前最佳)对比:")
    print(f"  V5(双层退坡/65线): +0.13%")
    print(f"  V6(分档/70线):     +0.07%")
    print(f"  V7(季节矩阵):      {total_return:+.2f}%")
    
    out_path = '/tmp/p6_backtest_v7_final.json'
    with open(out_path, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结果写入: {out_path}")
    print(f"⏱ 耗时: {elapsed:.0f}s")

if __name__ == '__main__':
    main()
