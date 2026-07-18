#!/usr/bin/env python3
"""
V13.3b 回测适配器
===================
目的：用V13.3b引擎重算历史评分（含价格下跌惩罚），然后跑全量回测。

方案：
1. 遍历2024-09-02 ~ 2026-07-16 每个交易日
2. 为每个交易日构建 MarketContext（从 season_state 表）
3. 用 V13.3b 引擎对当日有数据的每只股票重新评分（composite_score + penalty）
4. 写入 bt_v133_score 表
5. 复用 V13.1-v2 回测框架读取 bt_v133_score + V13.2 参数矩阵 → 运行回测

用法：
  # 第一步：重算历史评分（约30-60分钟，可后台运行）
  python3 backtest_v133_adapter.py recalc 2024-09-02 2026-07-16
  
  # 第二步：跑回测
  python3 backtest_v133_adapter.py backtest
"""

import sys, os, time, math, pymysql
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 数据库 ──
from db_config import _get_db_config, get_connection

# ── V13.3b 引擎组件 ──
from p6_dual_track_engine import (
    MarketContext, track_momentum, track_reversion, 
    _is_strong_stock, _build_calib_map, _apply_calibration,
    calibrate_scores
)

# ── SeasonEngine ──
from season_engine import SeasonEngine

# ====================================================================
# 第一部分：历史评分重算
# ====================================================================

def build_context_from_season_row(trade_date: str, season_info: dict) -> MarketContext:
    """从季节记录构建MarketContext"""
    judge_result = {
        'market_season': season_info.get('season', 'chaos'),
        'market_regime': season_info.get('regime', 'range'),
        'market_confidence': float(season_info.get('confidence', 0.5) or 0.5),
        'market_scoring_strategy': season_info.get('scoring_strategy', 'momentum'),
        'trade_date': trade_date,
    }
    return MarketContext(judge_result)


def recalc_scores(start_date: str, end_date: str):
    """步骤1：遍历历史交易日，用V13.3b引擎重算评分"""
    t0 = time.time()
    conn = get_connection()
    cur = conn.cursor()
    
    # 1. 读取所有交易日季节
    cur.execute("""
        SELECT trade_date, season, regime, confidence, scoring_strategy
        FROM season_state
        WHERE index_code='MARKET' AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date
    """, (start_date, end_date))
    
    season_days = {}
    for r in cur.fetchall():
        td = str(r['trade_date'])
        season_days[td] = {
            'season': r['season'],
            'regime': r.get('regime', 'range'),
            'confidence': float(r['confidence'] or 0.5),
            'scoring_strategy': r.get('scoring_strategy', 'momentum'),
        }
    print(f"📅 季节数据: {len(season_days)}个交易日")
    
    # 2. 确保bt_v133_score表存在
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bt_v133_score (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            trade_date DATE NOT NULL,
            composite_score DECIMAL(6,1),
            calibrated_score DECIMAL(6,1),
            track VARCHAR(20),
            penalty_score DECIMAL(6,1),
            penalty_reason VARCHAR(255),
            season VARCHAR(20),
            UNIQUE KEY uk_code_date (ts_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    
    # 3. 遍历每个交易日
    trading_days = sorted(season_days.keys())
    total_days = len(trading_days)
    total_scored = 0
    total_errors = 0
    
    print(f"\n🚀 开始重算V13.3b历史评分: {start_date} ~ {end_date} ({total_days}天)")
    
    for day_idx, td in enumerate(trading_days):
        if td not in season_days:
            continue
        
        day_start = time.time()
        ctx = build_context_from_season_row(td, season_days[td])
        
        # 获取该交易日有K线数据的股票
        cur.execute("""
            SELECT DISTINCT ts_code FROM daily_kline 
            WHERE trade_date=%s AND close>0
        """, (td,))
        all_codes = [r['ts_code'] for r in cur.fetchall()]
        
        if not all_codes:
            continue
        
        # 批量评分
        day_scores = []
        codes_batch = all_codes  # 全部一起
        
        for code in codes_batch:
            try:
                result = score_stock_v133(code, ctx)
                day_scores.append(result)
            except Exception as e:
                total_errors += 1
                continue
        
        if not day_scores:
            continue
        
        # 百分位校准
        calibrate_scores(day_scores)
        
        # 批量写入bt_v133_score表
        insert_sql = """
            INSERT INTO bt_v133_score 
                (ts_code, trade_date, composite_score, calibrated_score, track, penalty_score, penalty_reason, season)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                composite_score=VALUES(composite_score),
                calibrated_score=VALUES(calibrated_score),
                penalty_score=VALUES(penalty_score),
                penalty_reason=VALUES(penalty_reason)
        """
        
        for r in day_scores:
            det = r.get('details', {}) or {}
            penalty = float(det.get('penalty_score', 0) or 0)
            reason = det.get('penalty_reason', '')
            
            try:
                cur.execute(insert_sql, (
                    r['ts_code'], td,
                    round(r.get('score', 50), 1),
                    round(r.get('calibrated_score', 50), 1),
                    r.get('track', 'momentum'),
                    round(penalty, 1),
                    reason[:200],
                    ctx.season
                ))
            except Exception:
                total_errors += 1
        
        conn.commit()
        total_scored += len(day_scores)
        
        elapsed = time.time() - day_start
        if (day_idx + 1) % 20 == 0 or day_idx == 0:
            pct = (day_idx + 1) / total_days * 100
            total_elapsed = time.time() - t0
            rate = total_scored / total_elapsed
            eta = (total_days - day_idx - 1) * (total_elapsed / (day_idx + 1))
            print(f"  📊 [{day_idx+1}/{total_days}] {td} | {len(day_scores)}只 | "
                  f"⏱{elapsed:.1f}s | 累计{total_scored}只 | "
                  f"⏱{rate:.0f}只/s | ETA {eta/60:.0f}min")
    
    conn.close()
    total_elapsed = time.time() - t0
    print(f"\n✅ 完成! {total_scored}只评分写入bt_v133_score")
    print(f"⏱ 总耗时: {total_elapsed/60:.1f}min | 错误: {total_errors}")
    print(f"⚡ 平均: {total_scored/total_elapsed:.0f}只/秒")


def score_stock_v133(ts_code: str, ctx: MarketContext) -> Dict:
    """
    在V13.3b引擎下评分单只股票
    与p6_dual_track_engine.score_stock相同，但跳过短期过滤层（慢且不影响composite_score）
    """
    if ctx.is_momentum_track():
        result = track_momentum(ts_code, ctx)
    else:
        if _is_strong_stock(ts_code, ctx):
            result = track_momentum(ts_code, ctx)
            result['track'] = 'momentum'
        else:
            result = track_reversion(ts_code, ctx)
    
    result['ts_code'] = ts_code
    return result


# ====================================================================
# 第二部分：回测（复用V13.1-v2参数矩阵，从bt_v133_score读评分）
# ====================================================================

# ========== V13.2 最终参数矩阵（2026-07-05 定案） ==========
SEASON_PARAMS = {
    'summer':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':18.0, 'max_pos':50, 'max_total':50},
    'spring':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'weak_spring':    {'buy':68, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'chaos_spring':   {'buy':72, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':20, 'max_total':35},
    'chaos':          {'buy':80, 'hold':25, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':20, 'max_total':30},
    'chaos_autumn':   {'buy':72, 'hold':20, 't1':8.0,  't2':6.0,  'trail':10.0, 'max_pos':15, 'max_total':20},
    'weak_autumn':    {'buy':70, 'hold':20, 't1':8.0,  't2':6.0,  'trail':12.0, 'max_pos':20, 'max_total':25},
    'autumn':         {'buy':68, 'hold':20, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':30, 'max_total':35},
    'winter':         {'buy':85, 'hold':10, 't1':5.0,  't2':4.0,  'trail':8.0,  'max_pos':5,  'max_total':10},
}

INIT_CAPITAL = 1_000_000
BUY_PER_DAY = 3
CHARGE_RATE = 0.0005


def confidence_scale(conf: float) -> float:
    if conf >= 0.70: return 1.0
    if conf >= 0.50: return 0.875
    if conf >= 0.30: return 0.625
    return 0.50


def load_bt133_scores(start_date='2024-09-02', end_date='2026-07-16'):
    """从bt_v133_score表加载评分数据"""
    t0 = time.time()
    conn = get_connection()
    cur = conn.cursor()
    
    # 加载季节
    cur.execute(
        "SELECT trade_date, season, confidence FROM season_state "
        "WHERE index_code='MARKET' AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
        (start_date, end_date)
    )
    seasons = {}
    for r in cur.fetchall():
        td = str(r['trade_date'])
        seasons[td] = {'season': r['season'], 'confidence': float(r['confidence'] or 0.5)}
    
    # 加载V13.3b评分
    cur.execute(
        "SELECT ts_code, trade_date, composite_score, calibrated_score, "
        "       penalty_score, penalty_reason, track, season "
        "FROM bt_v133_score "
        "WHERE trade_date>=%s AND trade_date<=%s AND composite_score IS NOT NULL "
        "ORDER BY trade_date, ts_code",
        (start_date, end_date)
    )
    
    daily_scores = defaultdict(dict)
    all_raws = defaultdict(list)
    penalty_stats = defaultdict(lambda: {'count': 0, 'total_penalty': 0.0})
    
    for r in cur.fetchall():
        td = str(r['trade_date'])
        cs = float(r['composite_score'])
        ps = float(r['penalty_score'] or 0)
        daily_scores[td][r['ts_code']] = {
            'composite': cs,
            'calibrated': float(r['calibrated_score']),
            'penalty': ps,
        }
        all_raws[td].append(cs)
        if ps > 0:
            penalty_stats[td]['count'] += 1
            penalty_stats[td]['total_penalty'] += ps
    
    # 行情
    c2 = conn.cursor()
    c2.execute(
        "SELECT ts_code, trade_date, close FROM daily_kline "
        "WHERE trade_date>=%s AND trade_date<=%s AND close>0 ORDER BY trade_date",
        (start_date, end_date)
    )
    close_map = defaultdict(dict)
    for r2 in c2.fetchall():
        close_map[str(r2['trade_date'])][r2['ts_code']] = float(r2['close'])
    c2.close(); conn.close()
    
    print(f"  ✓ V13.3b评分: {len(daily_scores)}天×平均{sum(len(v) for v in daily_scores.values())//max(len(daily_scores),1)}只 ({time.time()-t0:.0f}s)")
    print(f"  ✓ 行情: {len(close_map)}天")
    
    return seasons, daily_scores, all_raws, close_map, penalty_stats


def run_backtest(start_date='2024-09-02', end_date='2026-07-16', 
                 label='V13.3b', compare_with_old=False):
    """
    用V13.3b评分运行回测
    如果compare_with_old=True，也会从strategy_signal读旧评分做对比回测
    """
    print(f"\n{'='*60}")
    print(f"📊 V13.3b 回测: {start_date} ~ {end_date}")
    print(f"{'='*60}")
    
    # 加载数据
    seasons, daily_scores, all_raws, close_map, penalty_stats = load_bt133_scores(start_date, end_date)
    
    # ── 回测 ──
    cash = INIT_CAPITAL
    positions = []
    all_trades = []
    portfolio_values = []
    
    t0 = time.time()
    trading_days = sorted(daily_scores.keys())
    print(f"  ✓ 交易日: {len(trading_days)}天")
    
    # 惩罚统计
    total_penalty_applied = 0.0
    penalty_count_days = 0
    
    for idx, td in enumerate(trading_days):
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
        
        # 当日惩罚统计
        if td in penalty_stats and penalty_stats[td]['count'] > 0:
            total_penalty_applied += penalty_stats[td]['total_penalty']
            penalty_count_days += 1
        
        # ── 检查持仓 ──
        conn = get_connection()
        cur = conn.cursor()
        new_positions = []
        for p in positions:
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
            if profit_pct <= -t1_pct:
                reason = f'止损T1({int(t1_pct*100)}%)'
            elif hold_days >= 2 and profit_pct <= -t2_pct:
                reason = f'止损T2({int(t2_pct*100)}%)'
            elif trail_pct > 0 and p['peak_price'] > p['buy_price']:
                dd_from_peak = (p['peak_price'] - cp) / p['peak_price']
                if dd_from_peak >= trail_pct:
                    reason = f'止盈({int(trail_pct*100)}%)'
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
        cur.close(); conn.close()
        
        # ── 买入 ──
        cur_pos_val = sum(p['cost'] for p in positions)
        max_total_val = INIT_CAPITAL * max_total_pct / 100.0
        
        if cur_pos_val < max_total_val and td in daily_scores and td in all_raws and all_raws[td]:
            day_data = daily_scores[td]
            raws = all_raws[td]
            
            candidates = []
            td_close = close_map.get(td, {})
            for code, data in day_data.items():
                cal = data.get('calibrated', 0)
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
        
        # ── 净值记录 ──
        pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
        portfolio_values.append((td, cash + pos_mkt))
        
        if (idx + 1) % 50 == 0:
            pos_vals = sum(p['cost'] for p in positions)
            print(f"  📅 {td} ({idx+1}/{len(trading_days)}) | 持仓{len(positions)} | "
                  f"¥{pos_vals/10000:.1f}万/¥{cash/10000:.0f}万 | {len(all_trades)}笔")
    
    # ── 结果计算 ──
    pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
    final_val = cash + pos_mkt
    total_ret = (final_val - INIT_CAPITAL) / INIT_CAPITAL * 100
    
    peak = INIT_CAPITAL
    max_dd = 0
    max_dd_date = ''
    for d, val in portfolio_values:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_date = d
    
    wins = [t for t in all_trades if t['profit_pct'] > 0]
    losses = [t for t in all_trades if t['profit_pct'] <= 0]
    
    print(f"\n{'='*60}")
    print(f"📊 {label} 回测结果 ({start_date} ~ {end_date})")
    print(f"{'='*60}")
    print(f"初始资金: ¥{INIT_CAPITAL/10000:.0f}万")
    print(f"最终资金: ¥{final_val/10000:.2f}万")
    print(f"总收益率: {total_ret:+.2f}%")
    print(f"最大回撤: {max_dd:.2f}% ({max_dd_date})")
    if max_dd > 0:
        print(f"卡玛比率: {total_ret/max_dd:.2f}x")
    print(f"交易笔数: {len(all_trades)}笔")
    print(f"胜率: {len(wins)/(len(wins)+len(losses))*100:.1f}% ({len(wins)}胜/{len(losses)}负)")
    if all_trades:
        avg_win_pct = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss_pct = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
        avg_pnl = sum(t['pnl'] for t in all_trades) / len(all_trades)
        avg_hold = sum(t['hold_days'] for t in all_trades) / len(all_trades)
        print(f"平均盈亏: ¥{avg_pnl:.0f} | 均持有: {avg_hold:.1f}d")
        print(f"平均盈利: {avg_win_pct:+.2f}% | 平均亏损: {avg_loss_pct:+.2f}%")
        if losses and avg_loss_pct != 0:
            print(f"盈亏比: {abs(avg_win_pct/avg_loss_pct):.2f}")
    
    # 惩罚统计
    if penalty_count_days > 0:
        print(f"\n📏 V13.3b 惩罚统计:")
        print(f"  有惩罚的交易日: {penalty_count_days}/{len(trading_days)} ({penalty_count_days/len(trading_days)*100:.0f}%)")
        print(f"  累计惩罚分: {total_penalty_applied:.0f} | 日均: {total_penalty_applied/penalty_count_days:.1f}")
    
    # 季节分析
    print(f"\n📂 按季节分析:")
    season_trades = defaultdict(list)
    for t in all_trades:
        season_trades[t['season']].append(t)
    for s in SEASON_PARAMS:
        ts = season_trades.get(s, [])
        if ts:
            sw = [t for t in ts if t['profit_pct'] > 0]
            avg_ret = sum(t['profit_pct'] for t in ts) / len(ts)
            avg_d = sum(t['hold_days'] for t in ts) / len(ts)
            print(f"  {s}: {len(ts)}笔 | {len(sw)/len(ts)*100:.0f}%胜率 | 均{avg_ret:+.2f}% | 均{avg_d:.0f}d")
    
    # 持有期分析
    print(f"\n📂 持有期分布:")
    for lo, hi in [(0,5),(5,10),(10,15),(15,20),(20,30),(30,60),(60,999)]:
        ts = [t for t in all_trades if lo <= t['hold_days'] < hi]
        if ts:
            sw = [t for t in ts if t['profit_pct'] > 0]
            avg_r = sum(t['profit_pct'] for t in ts) / len(ts)
            print(f"  {lo}-{hi}d: {len(ts)}笔 | {len(sw)/len(ts)*100:.0f}% | 均{avg_r:+.2f}%")
    
    print(f"\n🏆 TOP5:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'], reverse=True)[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
    
    print(f"\n💀 BOTTOM5:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
    
    # 留存持仓
    if positions:
        print(f"\n💼 留存持仓 ({len(positions)}只):")
        for p in positions:
            print(f"  {p['ts_code']} 买入{p['buy_date']} ¥{p['buy_price']:.2f} {p['shares']}股 ¥{p['cost']:.0f}")
    
    total_elapsed = time.time() - t0
    print(f"\n⏱ 耗时: {total_elapsed/60:.1f}min")
    
    # 返回关键指标
    return {
        'label': label,
        'total_return': total_ret,
        'max_drawdown': max_dd,
        'carmar': total_ret / max_dd if max_dd > 0 else 0,
        'trades': len(all_trades),
        'win_rate': len(wins)/(len(wins)+len(losses))*100 if all_trades else 0,
        'profit_factor': abs(sum(t['pnl'] for t in wins) / sum(abs(t['pnl']) for t in losses)) if losses and sum(abs(t['pnl']) for t in losses) > 0 else 0,
        'avg_hold': sum(t['hold_days'] for t in all_trades) / len(all_trades) if all_trades else 0,
    }


# ====================================================================
# 入口
# ====================================================================

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'recalc':
        start = sys.argv[2] if len(sys.argv) > 2 else '2024-09-02'
        end = sys.argv[3] if len(sys.argv) > 3 else '2026-07-16'
        recalc_scores(start, end)
    elif len(sys.argv) > 1 and sys.argv[1] == 'backtest':
        start = sys.argv[2] if len(sys.argv) > 2 else '2024-09-02'
        end = sys.argv[3] if len(sys.argv) > 3 else '2026-07-16'
        run_backtest(start, end, label='V13.3b')
    else:
        print("用法:")
        print("  python3 backtest_v133_adapter.py recalc [开始日期] [结束日期]")
        print("  python3 backtest_v133_adapter.py backtest [开始日期] [结束日期]")
