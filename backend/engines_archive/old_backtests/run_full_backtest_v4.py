#!/usr/bin/env python3
"""
P6双轨引擎 全量回测 V4（真实引擎版）
=======================================
直接调用 p6_dual_track_engine 的评分函数，
对回测池326只股票从2023-01-03到2026-06-09逐日模拟交易。

策略参数同V3: 买入线75, 最多6只, 100万, P1门限60, 延判2天, 止损衰减

运行: cd /root/stock-system-v2/backend && python3 run_full_backtest_v4.py
输出: /tmp/p6_backtest_v4_full.json
"""
import sys, os, json, time, math
from datetime import date, timedelta
from collections import defaultdict

# 确保找到引擎
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p6_dual_track_engine import score_stock, MarketContext, daily_pipeline, _build_calib_map, _apply_calibration
from season_engine import SeasonEngine
from db_config import get_connection
import pymysql

# ─── 策略参数 ───
# ─── 参数 ───
BUY_THRESHOLD = 70
MAX_POSITIONS = 8
POOL_MONEY = 1_000_000
HOLD_LIMIT = 30
COOL_DAYS = 20
# 分档参数
TIER1_MIN = 75       # 优等生：买入线+双层退坡(3天观察)-10%止损
TIER2_MIN = 70       # 普通生：谨慎买入+2天观察-7%止损
P1_TH = 60          # 评分退坡参考门限
P1_GRACE = 2        # 延判天数
# 止损（分档后动态，这里的值只用于跌不动的兜底）
SL_TIME_DECAY = [(5, 10), (7, 999), (8, 999)]
TS_PCT = 12

START_DATE = '2023-01-03'
END_DATE = '2026-06-09'

class Position:
    __slots__ = ('ts_code','buy_date','buy_price','score','hold_days',
                 'current_price','highest_price','exit_reason','sell_date','warning_day',
                 'buy_tier','hold_limit_extended','extended_hold_limit')
    def __init__(self, ts_code, buy_date, buy_price, score, buy_tier=2):
        self.ts_code = ts_code
        self.buy_date = buy_date
        self.buy_price = buy_price
        self.score = score
        self.buy_tier = buy_tier  # 1=Tier1(75+), 2=Tier2(70-75)
        self.hold_days = 0
        self.current_price = buy_price
        self.highest_price = buy_price
        self.exit_reason = None
        self.sell_date = None
        self.warning_day = 0
        self.hold_limit_extended = False
        self.extended_hold_limit = 0

def main():
    print("=" * 60)
    print("P6双轨引擎 V4 全量回测（资金因子修复 + 真实引擎评分）")
    print(f"参数: 买入线{BUY_THRESHOLD} 最多{MAX_POSITIONS}只 P1门限{P1_TH}")
    print(f"周期: {START_DATE} ~ {END_DATE}")
    print("=" * 60)
    t0 = time.time()
    
    # 1. 获取交易日和回测池
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
                (START_DATE, END_DATE))
    trade_dates = [str(r['trade_date']) for r in cur.fetchall()]
    
    cur.execute("SELECT ts_code FROM backtest_pool WHERE `status`='ACTIVE' AND market!='指数'")
    backtest_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    
    print(f"📋 {len(backtest_codes)}只股票 × {len(trade_dates)}天")
    
    # 2. 预加载所有日K线收盘价（快速查询用）
    conn = get_connection()
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
    
    # 3. 预计算季节判定（每天一次）
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
        
        if (idx+1) % 50 == 0:
            elapsed = time.time() - t0
            pct = (idx+1) / total_checks * 100
            eta = elapsed / (idx+1) * (total_checks - idx - 1)
            print(f"\r  [{idx+1}/{total_checks}] {pct:.0f}% 持仓{len(positions)}只 交易{len(recorder)}笔 "
                  f"ETA {eta:.0f}s", end='', flush=True)
        
        # 获取当日K线
        close_td = close_cache.get(td, {})
        
        # ── 检视持仓 ──
        for pos in list(positions):
            pos.hold_days += 1
            close = close_td.get(pos.ts_code)
            if close is None:
                continue
            pos.current_price = close
            
            # 获取当日评分
            try:
                sc = score_stock(pos.ts_code, ctx)
                score_now = sc['score']
            except:
                score_now = None
            
            # ───── 分档止损 ─────
            if pos.buy_tier == 1:
                # Tier1(75+): -10%止损
                stop_price = pos.buy_price * 0.90
                sl_label = '10'
            else:
                # Tier2(70-75): -7%止损
                stop_price = pos.buy_price * 0.93
                sl_label = '7'
            
            if close <= stop_price:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                    'sell_date':td,'hold_days':pos.hold_days,
                    'return_pct':round(ret,2),'exit_reason':f'T{pos.buy_tier}止损-{sl_label}%'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
                continue
            
            # ───── 移动止盈（统一12%）─────
            if close > pos.highest_price:
                pos.highest_price = close
            if close <= pos.highest_price * (1 - TS_PCT/100) and pos.hold_days >= 3:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                    'sell_date':td,'hold_days':pos.hold_days,
                    'return_pct':round(ret,2),'exit_reason':f'T{pos.buy_tier}止盈回撤{TS_PCT}%盈{ret:.1f}%'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
                continue
            
            # ───── P2: 30日平仓（评分>60则延期5天）─────
            if pos.hold_days >= HOLD_LIMIT:
                # 检查是否可以延期
                extend = False
                if score_now is not None and score_now >= 60:
                    extend = True
                
                if not extend:
                    ret = (close - pos.buy_price) / pos.buy_price * 100
                    recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                        'sell_date':td,'hold_days':pos.hold_days,
                        'return_pct':round(ret,2),'exit_reason':'P4平仓(30日)'})
                    cash += close
                    positions.remove(pos)
                    cool_until[pos.ts_code] = td
                    continue
                else:
                    # 延期5天，设置新的持有上限
                    pos.hold_limit_extended = True
                    pos.extended_hold_limit = HOLD_LIMIT + 5
            
            # 如果已延期，检查延期后的强制平仓
            if hasattr(pos, 'hold_limit_extended') and pos.hold_limit_extended:
                if pos.hold_days >= pos.extended_hold_limit:
                    ret = (close - pos.buy_price) / pos.buy_price * 100
                    recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                        'sell_date':td,'hold_days':pos.hold_days,
                        'return_pct':round(ret,2),'exit_reason':'P4延期平仓(35日)'})
                    cash += close
                    positions.remove(pos)
                    cool_until[pos.ts_code] = td
                    continue
            
            # ───── P1: 双层退坡（分档差异化）─────
            if score_now is not None and pos.hold_days >= P1_GRACE:
                if pos.buy_tier == 1:
                    # Tier1(75+): 3天观察期，跌破55退
                    if score_now < 60:
                        pos.warning_day += 1
                        if pos.warning_day >= 3:
                            if score_now < 55:
                                ret = (close - pos.buy_price) / pos.buy_price * 100
                                recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                                    'sell_date':td,'hold_days':pos.hold_days,
                                    'return_pct':round(ret,2),'exit_reason':f'T1双层跌破55({score_now})'})
                                cash += close
                                positions.remove(pos)
                                cool_until[pos.ts_code] = td
                            elif score_now >= 55 and score_now < 60 and close <= pos.buy_price * 1.02:
                                ret = (close - pos.buy_price) / pos.buy_price * 100
                                recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                                    'sell_date':td,'hold_days':pos.hold_days,
                                    'return_pct':round(ret,2),'exit_reason':f'T1双层价确认({score_now})'})
                                cash += close
                                positions.remove(pos)
                                cool_until[pos.ts_code] = td
                    else:
                        # 评分回升到60+，重置观察状态
                        pos.warning_day = 0
                else:
                    # Tier2(70-75): 2天观察期，跌破60退
                    if score_now < 60:
                        pos.warning_day += 1
                        if pos.warning_day >= 2:
                            ret = (close - pos.buy_price) / pos.buy_price * 100
                            recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
                                'sell_date':td,'hold_days':pos.hold_days,
                                'return_pct':round(ret,2),'exit_reason':f'T2双层跌破60({score_now})'})
                            cash += close
                            positions.remove(pos)
                            cool_until[pos.ts_code] = td
                    else:
                        pos.warning_day = 0
        
        # ── 买入（分档）──
        if len(positions) < MAX_POSITIONS:
            candidates = []
            for code in backtest_codes:
                if code in cool_until and cool_until[code] >= td:
                    continue
                if any(p.ts_code == code for p in positions):
                    continue
                try:
                    sc = score_stock(code, ctx)
                    score_val = sc['score']
                    if score_val >= BUY_THRESHOLD:
                        close = close_td.get(code)
                        if close:
                            # 判断分档
                            tier = 1 if score_val >= TIER1_MIN else 2
                            candidates.append((code, score_val, close, tier))
                except:
                    pass
            
            candidates.sort(key=lambda x: x[1], reverse=True)
            for code, score, close, tier in candidates[:MAX_POSITIONS - len(positions)]:
                if any(p.ts_code == code for p in positions):
                    continue
                pos = Position(code, td, close, score, buy_tier=tier)
                positions.append(pos)
                cash -= close
    
    # 期末平仓
    last_td = trade_dates[-1]
    for pos in positions:
        close = close_cache.get(last_td, {}).get(pos.ts_code, pos.current_price)
        ret = (close - pos.buy_price) / pos.buy_price * 100
        recorder.append({'ts_code':pos.ts_code,'buy_date':pos.buy_date,
            'sell_date':last_td,'hold_days':pos.hold_days,
            'return_pct':round(ret,2),'exit_reason':'期末平仓'})
        cash += close
    
    elapsed = time.time() - t0
    print(f"\r  [{total_checks}/{total_checks}] 100% 交易{len(recorder)}笔")
    
    # ─── 统计 ───
    trades = recorder
    win = [t for t in trades if t['return_pct'] > 0]
    lose = [t for t in trades if t['return_pct'] <= 0]
    
    total_return = (cash - POOL_MONEY) / POOL_MONEY * 100
    years = len(trade_dates) / 244
    
    # 持仓区间
    buckets = [(1,5,'1-5日'),(6,10,'6-10日'),(11,15,'11-15日'),
               (16,20,'16-20日'),(21,30,'21-30日'),(31,60,'31-60日')]
    hold_stats = {}
    for lo, hi, key in buckets:
        items = [t for t in trades if lo <= t['hold_days'] <= hi]
        if items:
            hold_stats[key] = {
                'count': len(items),
                'avg_return': round(sum(t['return_pct'] for t in items)/len(items), 2),
                'win_rate': round(len([t for t in items if t['return_pct']>0])/len(items)*100, 1),
            }
    
    exit_stats = defaultdict(lambda: {'count':0,'sum_ret':0})
    for t in trades:
        exit_stats[t['exit_reason']]['count'] += 1
        exit_stats[t['exit_reason']]['sum_ret'] += t['return_pct']
    
    avg_hd = sum(t['hold_days'] for t in trades)/len(trades) if trades else 0
    avg_win = sum(t['return_pct'] for t in win)/len(win) if win else 0
    avg_lose = sum(t['return_pct'] for t in lose)/len(lose) if lose else 0
    pf = abs(sum(t['return_pct'] for t in win)/sum(t['return_pct'] for t in lose)) if lose else 999
    
    returns = [t['return_pct'] for t in trades]
    avg_r = sum(returns)/len(returns) if returns else 0
    std_r = math.sqrt(sum((r-avg_r)**2 for r in returns)/len(returns)) if len(returns) > 1 else 1
    sharpe = (avg_r/std_r)*math.sqrt(244) if std_r > 0 else 0
    
    result = {
        'strategy': 'V6_分档策略_买入线70_75+10%止损_70+7%止损_双层退坡P2延期_P1_60_延判2天',
        'params': {
            'buy_threshold': BUY_THRESHOLD, 'max_positions': MAX_POSITIONS,
            'pool_money': POOL_MONEY, 'hold_limit': HOLD_LIMIT,
            'p1': P1_TH, 'p1_grace': P1_GRACE, 'ts_pct': TS_PCT,
        },
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
        'exit_reasons': {k:{'count':v['count'],'avg_return':round(v['sum_ret']/v['count'],2)} for k,v in exit_stats.items()},
    }
    
    print(f"\n{'='*60}")
    print(f"📊 全量回测结果（资金因子修复 + 真实引擎）")
    print(f"{'='*60}")
    print(f"总收益率: {total_return:.2f}%")
    print(f"年化收益: {result['annual_return_pct']:.2f}%")
    print(f"交易笔数: {len(trades)}笔")
    print(f"胜率: {result['win_rate']}%")
    print(f"均盈: {avg_win:.2f}% | 均亏: {avg_lose:.2f}%")
    print(f"盈利因子: {pf:.2f}")
    print(f"夏普: {sharpe:.2f}")
    print(f"平均持仓: {avg_hd:.1f}天")
    print(f"\n持仓区间:")
    for k,v in sorted(hold_stats.items(), key=lambda x: int(x[0].split('日')[0].split('-')[0])):
        print(f"  {k}: {v['count']}笔 均收益{v['avg_return']}% 胜率{v['win_rate']}%")
    
    print(f"\n=== V3 vs V4 对比 ===")
    print(f"{'指标':<16} {'V3(旧引擎)':<16} {'V4(资金因子修复)':<16}")
    print(f"{'='*48}")
    print(f"{'交易笔数':<16} {'36':<16} {str(len(trades)):<16}")
    print(f"{'胜率':<16} {'33.33%':<16} f\"{result['win_rate']:.2f}%\":<16")
    print(f"{'均收益':<16} {'14.36%':<16} f\"{avg_win:.2f}%\":<16")
    print(f"{'盈利因子':<16} {'0.98':<16} f\"{pf:.2f}\":<16")
    print(f"{'总收益率':<16} {'-0.72%':<16} f\"{total_return:.2f}%\":<16")
    
    out_path = '/tmp/p6_backtest_v4_full.json'
    with open(out_path, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结果写入: {out_path}")
    print(f"⏱ 耗时: {elapsed:.0f}s")

if __name__ == '__main__':
    main()
