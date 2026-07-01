#!/usr/bin/env python3
"""
融资融券 + daily_basic 数据批量拉取脚本
========================================
1. margin_detail：回测池344只股票，2023-01-03 ~ 最新
2. daily_basic：回测池股票，补充换手率数据

用法: python3 fetch_margin_and_basic.py
"""
import tushare as ts
import pymysql
import time
import sys
from datetime import date, datetime, timedelta

# ============================================================
# 配置
# ============================================================
MYSQL_PWD = 'iXve1rVBXfdA4tL9'

# ============================================================
# 数据库连接
# ============================================================
def get_conn():
    return pymysql.connect(
        host='localhost', user='debian-sys-maint', password=MYSQL_PWD,
        db='stock_db_v2', charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )

# ============================================================
# 1. 拉取融资融券 margin_detail
# ============================================================
def pull_margin():
    pro = ts.pro_api()
    conn = get_conn()
    cur = conn.cursor()

    # 获取回测池股票
    cur.execute("SELECT DISTINCT ts_code FROM backtest_pool")
    pool = [r['ts_code'] for r in cur.fetchall()]
    print(f"[margin] 回测池股票: {len(pool)}只")

    # 获取已有数据
    cur.execute("SELECT ts_code, MAX(trade_date) as max_dt FROM margin_detail GROUP BY ts_code")
    existing = {r['ts_code']: r['max_dt'] for r in cur.fetchall()}
    print(f"[margin] 已有数据: {len(existing)}只")

    # 统计
    total_inserted = 0
    total_failed = 0
    total_none = 0
    t_start = time.time()

    for i, code in enumerate(pool):
        last_dt = existing.get(code)
        if last_dt:
            start_dt = (last_dt + timedelta(days=1)).strftime('%Y%m%d')
            # 如果最新数据就是今天或昨天，跳过
            if start_dt >= datetime.now().strftime('%Y%m%d'):
                continue
        else:
            start_dt = '20230103'

        try:
            df = pro.margin_detail(ts_code=code, start_date=start_dt, end_date='20260612', limit=6000)
            if df is None or len(df) == 0:
                total_none += 1
                if (i+1) % 50 == 0:
                    print(f"  [{i+1}/{len(pool)}] {code}: 无数据")
                continue

            # 批量插入
            records = []
            for _, row in df.iterrows():
                records.append((
                    row['ts_code'],
                    row['trade_date'],
                    float(row['rzye']) if row['rzye'] else None,
                    float(row['rqye']) if row['rqye'] else None,
                    float(row['rzmre']) if row['rzmre'] else None,
                    float(row['rqyl']) if row['rqyl'] else None,
                    float(row['rzche']) if row['rzche'] else None,
                    float(row['rqchl']) if row['rqchl'] else None,
                    float(row['rqmcl']) if row['rqmcl'] else None,
                    float(row['rzrqye']) if row['rzrqye'] else None,
                ))

            sql = """INSERT IGNORE INTO margin_detail 
            (ts_code, trade_date, rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""

            cur.executemany(sql, records)
            conn.commit()
            total_inserted += len(records)

            if (i+1) % 50 == 0:
                elapsed = time.time() - t_start
                print(f"  [{i+1}/{len(pool)}] {code}: {len(records)}条 | 累计{total_inserted}条 | {elapsed:.0f}s")

        except Exception as e:
            total_failed += 1
            print(f"  [{i+1}/{len(pool)}] {code}: 错误 - {e}")

        # 流控：每请求间隔0.15秒（6000积分每分钟500次=每0.12秒1次）
        time.sleep(0.15)

    elapsed = time.time() - t_start
    cur.close()
    conn.close()
    print(f"\n[margin] 完成！插入{total_inserted}条, 无数据{total_none}只, 失败{total_failed}只, 耗时{elapsed:.0f}s")
    return total_inserted


# ============================================================
# 2. 拉取 daily_basic（换手率/PE/PB等）
# ============================================================
def pull_daily_basic():
    pro = ts.pro_api()
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT ts_code FROM backtest_pool")
    pool = [r['ts_code'] for r in cur.fetchall()]

    # 已有数据
    cur.execute("SELECT MIN(trade_date) as dmin, MAX(trade_date) as dmax, COUNT(*) as cnt FROM daily_basic WHERE ts_code IN (SELECT ts_code FROM backtest_pool)")
    r = cur.fetchone()
    print(f"\n[daily_basic] 已有: {r['cnt']}条, {r['dmin']} ~ {r['dmax']}")

    # 按日全市场拉取（5512只/日），回测范围：2023-01-03 ~ 最新
    # 已有316条，说明之前只拉了一点点。补拉到2023年
    t_start = time.time()
    total_inserted = 0
    total_skip = 0
    total_fail = 0

    cur.execute("SELECT MIN(trade_date) as min_d, MAX(trade_date) as max_d FROM daily_kline_qfq")
    dr = cur.fetchone()
    date_from = str(dr["min_d"]) if dr and dr["min_d"] else "2023-01-03"
    date_to = str(dr["max_d"]) if dr and dr["max_d"] else "2026-06-12"
    # 获取交易日历
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date", (date_from, date_to))
    trade_dates_all = [r['trade_date'].strftime('%Y%m%d') for r in cur.fetchall()]
    
    # 已有日期
    cur.execute("SELECT DISTINCT trade_date FROM daily_basic WHERE trade_date BETWEEN %s AND %s", (date_from, date_to))
    existing_dates = set(r['trade_date'].strftime('%Y%m%d') for r in cur.fetchall())
    
    todo_dates = [d for d in trade_dates_all if d not in existing_dates]
    print(f"[daily_basic] 交易日: {len(trade_dates_all)}个, 已有: {len(existing_dates)}个, 待拉: {len(todo_dates)}个")

    for i, dt in enumerate(todo_dates):
        try:
            df = pro.daily_basic(trade_date=dt)
            if df is None or len(df) == 0:
                total_skip += 1
                continue

            # 只保留回测池的股票
            df_pool = df[df['ts_code'].isin(pool)]
            if len(df_pool) == 0:
                if (i+1) % 50 == 0:
                    print(f"  [{i+1}/{len(todo_dates)}] {dt}: 无回测池数据")
                continue

            # 批量插入
            records = []
            for _, row in df_pool.iterrows():
                records.append((
                    row['ts_code'],
                    row['trade_date'],
                    float(row['turnover_rate']) if row['turnover_rate'] else None,
                    float(row['turnover_rate_f']) if row['turnover_rate_f'] else None,
                    float(row['pe_ttm']) if row['pe_ttm'] else None,
                    float(row['pb']) if row['pb'] else None,
                ))

            sql = """INSERT IGNORE INTO daily_basic 
            (ts_code, trade_date, turnover_rate, turnover_rate_f, pe_ttm, pb)
            VALUES (%s, %s, %s, %s, %s, %s)"""

            cur.executemany(sql, records)
            conn.commit()
            total_inserted += len(records)

            if (i+1) % 50 == 0:
                elapsed = time.time() - t_start
                print(f"  [{i+1}/{len(todo_dates)}] {dt}: {len(records)}条/日 | 累计{total_inserted} | {elapsed:.0f}s")

        except Exception as e:
            total_fail += 1
            if (i+1) % 50 == 0:
                print(f"  [{i+1}/{len(todo_dates)}] {dt}: {e}")

        # 流控
        time.sleep(0.12)

    elapsed = time.time() - t_start
    cur.close()
    conn.close()
    print(f"\n[daily_basic] 完成！插入{total_inserted}条, 跳过{total_skip}天, 失败{total_fail}天, 耗时{elapsed:.0f}s")
    return total_inserted


# ============================================================
# 主流程
# ============================================================
if __name__ == '__main__':
    print("="*60)
    print("融资融券 + daily_basic 数据拉取")
    print("="*60)

    print("\n>>> 第一阶段：拉取融资融券 margin_detail <<<")
    m = pull_margin()

    print("\n>>> 第二阶段：拉取 daily_basic（换手率） <<<")
    d = pull_daily_basic()

    print("\n" + "="*60)
    print(f"全部完成！margin_detail: {m}条, daily_basic: {d}条")
    print("="*60)
