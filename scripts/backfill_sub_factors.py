#!/usr/bin/env python3
"""
backfill_sub_factors.py — 补全 strategy_signal 中子因子缺失的数据

问题背景:
p6_dual_track_engine 在 scoring_strategy='momentum' 的轨道中，
部分股票因 money_flow/technical_indicator 数据不足走 exception 路径，
返回了 {'score':50, 'details':None}。
导致 strategy_signal 中 composite_score 正确但 trend/momentum/structure/mf 等子因子为0。

修复方法:
对于 trend_score=0 且 composite_score>0 的记录，
从 chanlun_structure 重新推算趋势分+结构分，
对于无法推算的，根据 composite_score 按 V13.1 公式反推合理值。

使用: python3 backfill_sub_factors.py [--dry-run]
"""

import pymysql, re, sys, argparse
from datetime import datetime

def get_password():
    pwd = open('/etc/mysql/debian.cnf').read()
    m = re.search(r'password\s*=\s*(\S+)', pwd)
    return m.group(1) if m else None

def get_conn(db='stock_db_v2'):
    pw = get_password()
    return pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint', password=pw, database=db, charset='utf8mb4')

def calc_trend_from_chanlun(cl_row):
    """从 chanlun_structure 行计算 trend_score (同track_momentum逻辑)"""
    trend_score = 50
    raw_structure = 50
    
    if cl_row and cl_row.get('structure_score') is not None:
        ss = float(cl_row['structure_score'])
        raw_structure = ss
        if ss >= 75: trend_score = 85
        elif ss >= 60: trend_score = 70
        elif ss >= 40: trend_score = 55
        else: trend_score = 35
        
        bs = cl_row.get('buy_sell_point', 'none')
        bs_boost = {'buy3': 15, 'buy2': 8, 'buy1': 3, 'sell3': -15, 'sell2': -8, 'sell1': -3}.get(bs, 0)
        trend_score = max(0, min(100, trend_score + bs_boost))
        
        bt = cl_row.get('beichi_type', 'none')
        if bt == 'bottom' and (cl_row.get('beichi_strength') or 0) > 40:
            trend_score = min(100, trend_score + 10)
        elif bt == 'top' and (cl_row.get('beichi_strength') or 0) > 40:
            trend_score = max(0, trend_score - 10)
    
    return trend_score, raw_structure

def calc_momentum_from_kline(ts_code, trade_date, cur):
    """从 daily_kline 计算 momentum_score"""
    cur.execute("""
        SELECT close, ma_5, ma_10, ma_20, ma_60, rsi_12, macd_dif, macd_dea
        FROM daily_kline d
        LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
        WHERE d.ts_code=%s AND d.trade_date <= %s
        ORDER BY d.trade_date DESC LIMIT 21
    """, (ts_code, trade_date))
    
    rows = cur.fetchall()
    if not rows or len(rows) < 20:
        return None
    
    closes = [float(r['close']) for r in reversed(rows)]
    n = len(closes)
    
    score = 50
    if n >= 21:
        r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
        r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0
        r20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0
        
        cons_up = 0
        for i in range(-5, 0):
            if closes[i] > closes[i-1]: cons_up += 1
            else: cons_up = 0
        
        rsi_val = float(rows[0].get('rsi_12', 50) or 50)
        
        score = 50
        score += max(-15, min(15, r5 * 150))
        score += max(-10, min(10, r10 * 80))
        score += max(-8, min(8, r20 * 50))
        score += min(8, cons_up * 2)
        score += (rsi_val - 50) * 0.5
        score = max(0, min(100, score))
    
    return round(score, 1)


def calc_mf_from_moneyflow(ts_code, trade_date, cur):
    """从 money_flow 计算 mf_score"""
    cur.execute("""
        SELECT net_value, main_net, buy_value
        FROM money_flow
        WHERE ts_code=%s AND trade_date=%s
    """, (ts_code, trade_date))
    r = cur.fetchone()
    if not r:
        return None
    
    net_value = float(r['net_value'] or 0)
    main_net = float(r['main_net'] or 0)
    mf_5d = float(r['buy_value'] or 0)
    
    mf_score = 50
    if abs(net_value) > 0:
        ratio = net_value / (abs(net_value) + 1e-8)
        mf_score += ratio * 30
    if abs(main_net) > 0:
        mf_score += (main_net / (abs(main_net) + 1e-8)) * 15
    mf_score = max(0, min(100, mf_score))
    
    return round(mf_score, 1)


def estimate_factors_from_composite(composite_score, has_chanlun_data):
    """当既无钱流也无技术指标时，从 composite_score 反推合理因子值"""
    # V13.1公式: composite = trend×0.40 + struct×0.10 + mom×0.25 + mf×0.25
    # 当无法计算时，用经验比例分配
    
    if not has_chanlun_data:
        # 纯均线趋势判断
        trend = 55
        struct = 40
    else:
        # 有缠论——中等结构分
        trend = max(35, min(85, composite_score * 0.9))
        struct = max(0, min(100, composite_score - 20))
    
    mom = max(30, min(90, composite_score * 0.85))
    mf = max(30, min(90, composite_score * 0.7))
    
    return round(trend, 1), round(struct, 1), round(mom, 1), round(mf, 1)


def backfill(dry_run=False):
    pw = get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint', password=pw, database='stock_db_v2', charset='utf8mb4')
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 1. 找到缺失子因子的记录
    cur.execute("""
        SELECT ts_code, trade_date, composite_score
        FROM strategy_signal
        WHERE trend_score=0 AND composite_score>0
          AND trade_date >= '2026-06-25'
        ORDER BY trade_date DESC, composite_score DESC
    """)
    missing = cur.fetchall()
    print(f"📋 找到 {len(missing)} 条缺失子因子的记录")
    
    # 按日期分组
    from collections import defaultdict
    by_date = defaultdict(list)
    for r in missing:
        by_date[str(r['trade_date'])].append(r)
    for d in sorted(by_date.keys()):
        print(f"  {d}: {len(by_date[d])}只")
    
    fixed = {'trend': 0, 'struct': 0, 'mom': 0, 'mf': 0}
    skipped = {'no_chanlun': 0, 'no_kline': 0, 'fallback': 0}
    
    for idx, r in enumerate(missing):
        ts_code = r['ts_code']
        trade_date = str(r['trade_date'])
        comp = float(r['composite_score'])
        
        # 2. 从 chanlun_structure 计算趋势分
        cur.execute("""
            SELECT structure_score, buy_sell_point, beichi_type, beichi_strength
            FROM chanlun_structure
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 1
        """, (ts_code, trade_date))
        cl_row = cur.fetchone()
        
        trend_score, struct_score = calc_trend_from_chanlun(cl_row)
        
        # 3. 从 daily_kline + technical_indicator 计算动量分
        momentum_score = calc_momentum_from_kline(ts_code, trade_date, cur)
        
        # 4. 从 money_flow 计算资金分
        mf_score = calc_mf_from_moneyflow(ts_code, trade_date, cur)
        
        has_chanlun = cl_row is not None and cl_row.get('structure_score') is not None
        
        # 5. 如果有缺少的，从 composite 反推
        if momentum_score is None or mf_score is None:
            est_t, est_s, est_mom, est_mf = estimate_factors_from_composite(comp, has_chanlun)
            if momentum_score is None:
                momentum_score = est_mom
                skipped['no_kline'] += 1
            if mf_score is None:
                mf_score = est_mf
                skipped['no_kline'] += 1
        
        if dry_run:
            if (idx+1) % 20 == 0:
                print(f"  [DRY-RUN] {idx+1}/{len(missing)} | {ts_code} | trend={trend_score} struct={struct_score} mom={momentum_score} mf={mf_score}")
            continue
        
        # 6. 写入 strategy_signal
        cur.execute("""
            UPDATE strategy_signal
            SET trend_score=%s, structure_score=%s, momentum_score=%s, mf_score=%s
            WHERE ts_code=%s AND trade_date=%s
        """, (trend_score, struct_score, momentum_score, mf_score, ts_code, trade_date))
        
        # 同时也写入 backtest_score_daily (如果存在)
        cur.execute("""
            UPDATE backtest_score_daily
            SET chanlun_trend=%s, structure_score=%s, momentum_score=%s, mf_score=%s
            WHERE ts_code=%s AND trade_date=%s
        """, (trend_score, struct_score, momentum_score, mf_score, ts_code, trade_date))
        
        fixed['trend'] += 1 if trend_score > 0 else 0
        fixed['struct'] += 1 if struct_score > 0 else 0
        fixed['mom'] += 1 if momentum_score > 0 else 0
        fixed['mf'] += 1 if mf_score > 0 else 0
        
        if (idx+1) % 50 == 0:
            conn.commit()
            print(f"  ⏳ {idx+1}/{len(missing)} 完成")
    
    conn.commit()
    cur.close()
    conn.close()
    
    print(f"\n✅ Backfill 完成!")
    if not dry_run:
        print(f"  趋势分已补: {fixed['trend']} 条")
        print(f"  结构分已补: {fixed['struct']} 条")
        print(f"  动量分已补: {fixed['mom']} 条")
        print(f"  资金分已补: {fixed['mf']} 条")
        print(f"  反推(无K线/钱流): {skipped['no_kline']} 次")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='补全strategy_signal子因子')
    parser.add_argument('--dry-run', action='store_true', help='不写库，仅预览')
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
