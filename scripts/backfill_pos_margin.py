#!/usr/bin/env python3
"""
backfill_pos_margin.py — 补全 strategy_signal 中 pos_score 和 margin_score

问题：
p6_dual_track_engine 的 daily_pipeline 之前 INSERT 时没有包含 pos_score 和 margin_score 字段，
导致7月2日/3日的记录这两个字段为0。

修复方法：
- pos_score: 从 daily_kline 取250日价格位置百分比
- margin_score: 从 margin_detail 取融资买入净额占比
"""

import pymysql, re, sys
from collections import defaultdict

def get_password():
    pwd = open('/etc/mysql/debian.cnf').read()
    m = re.search(r'password\s*=\s*(\S+)', pwd)
    return m.group(1)

def main(dry_run=False):
    PASSWORD = get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint', password=PASSWORD, database='stock_db_v2', charset='utf8mb4')
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 找到pos_score=0或margin_score=0的记录
    cur.execute("""
        SELECT ts_code, trade_date, composite_score, pos_score, margin_score
        FROM strategy_signal
        WHERE trade_date >= '2026-06-25'
          AND (pos_score IS NULL OR pos_score = 0 OR margin_score IS NULL OR margin_score = 0)
        ORDER BY trade_date DESC
    """)
    missing = cur.fetchall()
    print(f"📋 找到 {len(missing)} 条缺失 pos/margin 的记录")
    
    # 按日期分组
    by_date = defaultdict(list)
    for r in missing:
        by_date[str(r['trade_date'])].append(r['ts_code'])
    for d in sorted(by_date.keys(), reverse=True):
        print(f"  {d}: {len(by_date[d])}只")
    
    if dry_run:
        print("\n🔍 Dry-run 模式，不写入")
        cur.close(); conn.close()
        return
    
    fixed_pos = 0
    fixed_margin = 0
    
    for idx, r in enumerate(missing):
        code = r['ts_code']
        td = str(r['trade_date'])
        needs_pos = r['pos_score'] is None or r['pos_score'] == 0
        needs_margin = r['margin_score'] is None or r['margin_score'] == 0
        
        pos_val = None
        margin_val = None
        
        # 1. pos_score: 250日价格位置
        if needs_pos:
            cur.execute("""
                SELECT d.close,
                       (SELECT MAX(close) FROM daily_kline WHERE ts_code=%s AND trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 250 DAY)) as hi,
                       (SELECT MIN(close) FROM daily_kline WHERE ts_code=%s AND trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 250 DAY)) as lo
                FROM daily_kline d
                WHERE d.ts_code=%s AND d.trade_date=%s
                LIMIT 1
            """, (code, td, td, code, td, td, code, td))
            pr = cur.fetchone()
            if pr and pr['close'] and pr['hi'] and pr['lo'] and pr['hi'] > pr['lo']:
                pos_val = round((float(pr['close']) - float(pr['lo'])) / (float(pr['hi']) - float(pr['lo'])) * 100, 1)
            else:
                pos_val = 50.0
        
        # 2. margin_score: 融资占比
        if needs_margin:
            cur.execute("""
                SELECT rzmre, rzche, rzrqye
                FROM margin_detail
                WHERE ts_code=%s AND trade_date=%s
                LIMIT 1
            """, (code, td))
            mr = cur.fetchone()
            if mr and mr['rzrqye'] and float(mr['rzrqye']) > 0:
                rzmre = float(mr['rzmre'] or 0)
                rzche = float(mr['rzche'] or 0)
                rzye = float(mr['rzrqye'])
                net_buy = rzmre - rzche
                if rzye > 1e8:
                    base = 50 + max(-20, min(20, net_buy / rzye * 1000))
                    margin_val = max(0, min(100, base))
                else:
                    margin_val = 40.0
            else:
                margin_val = 30.0
        
        # 3. UPDATE
        updates = []
        params = []
        if needs_pos and pos_val is not None:
            updates.append("pos_score=%s")
            params.append(pos_val)
        if needs_margin and margin_val is not None:
            updates.append("margin_score=%s")
            params.append(margin_val)
        
        if updates:
            params += [code, td]
            sql = f"UPDATE strategy_signal SET {', '.join(updates)} WHERE ts_code=%s AND trade_date=%s"
            cur.execute(sql, params)
            if needs_pos: fixed_pos += 1
            if needs_margin: fixed_margin += 1
        
        if (idx+1) % 100 == 0:
            conn.commit()
            print(f"  ⏳ {idx+1}/{len(missing)} 完成")
    
    conn.commit()
    cur.close()
    conn.close()
    
    print(f"\n✅ Backfill 完成!")
    print(f"  pos_score 已补: {fixed_pos} 条")
    print(f"  margin_score 已补: {fixed_margin} 条")

if __name__ == '__main__':
    dr = '--dry-run' in sys.argv
    main(dry_run=dr)
