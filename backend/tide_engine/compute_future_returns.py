#!/usr/bin/env python3
"""
计算所有 tide_score_signal 的未来收益（5/10/20日）
与生成回测统计报告
"""
import sys, os, time
from datetime import datetime, date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_config import get_connection

def compute_future_returns():
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) AS cnt FROM tide_score_signal")
    total = cur.fetchone()['cnt']
    print(f'共 {total} 条评分记录')
    
    cur.execute("SELECT trade_date, COUNT(*) cnt FROM tide_score_signal GROUP BY trade_date ORDER BY trade_date")
    print('已有评分日期:')
    for r in cur.fetchall():
        print(f'  {r["trade_date"]}: {r["cnt"]}条')
    
    # 取K线
    print('\n加载K线数据...')
    cur.execute("SELECT ts_code, trade_date, close FROM daily_kline ORDER BY ts_code, trade_date")
    kline_rows = cur.fetchall()
    
    kline_map = {}
    for r in kline_rows:
        code = r['ts_code']
        if code not in kline_map:
            kline_map[code] = []
        kline_map[code].append((str(r['trade_date']), float(r['close'])))
    print(f'K线数据: {len(kline_map)}只股票')
    
    # 计算未来收益
    cur.execute("""
        SELECT id, ts_code, trade_date, tide_score
        FROM tide_score_signal
        WHERE future_return_5d IS NULL
        ORDER BY trade_date, ts_code
    """)
    pending = cur.fetchall()
    print(f'待计算收益: {len(pending)}条')
    
    updated = 0
    t0 = time.time()
    for sig in pending:
        sig_id = sig['id']
        code = sig['ts_code']
        sig_date = str(sig['trade_date'])
        if code not in kline_map:
            continue
        dates_arr = kline_map[code]
        idx = next((i for i, (d, _) in enumerate(dates_arr) if d == sig_date), None)
        if idx is None:
            continue
        base = dates_arr[idx][1]
        
        def future_ret(steps):
            tgt = idx + steps
            if tgt < len(dates_arr):
                return (dates_arr[tgt][1] - base) / base * 100
            return None
        
        cur.execute("""
            UPDATE tide_score_signal 
            SET future_return_5d=%s, future_return_10d=%s, future_return_20d=%s
            WHERE id=%s
        """, (future_ret(5), future_ret(10), future_ret(20), sig_id))
        updated += 1
    
    conn.commit()
    elapsed = time.time() - t0
    print(f'已更新 {updated} 条, 耗时 {elapsed:.1f}s')
    
    # 统计
    cur.execute("""
        SELECT COUNT(*) cnt,
               AVG(CASE WHEN tide_score >= 60 THEN future_return_5d END) buy_r5,
               AVG(CASE WHEN tide_score >= 60 THEN future_return_10d END) buy_r10,
               AVG(CASE WHEN tide_score >= 60 THEN future_return_20d END) buy_r20,
               AVG(CASE WHEN tide_score < 60 THEN future_return_5d END) avoid_r5,
               AVG(CASE WHEN tide_score < 60 THEN future_return_10d END) avoid_r10,
               AVG(CASE WHEN tide_score < 60 THEN future_return_20d END) avoid_r20
        FROM tide_score_signal
        WHERE future_return_5d IS NOT NULL
    """)
    r = cur.fetchone()
    print(f'\n📊 Tide评分回测结果:')
    print(f'  {"":>12} {"R5%":>10} {"R10%":>10} {"R20%":>10}')
    print(f'  {"买入(≥60)":>12} {float(r["buy_r5"]):>10.2f}% {float(r["buy_r10"]):>10.2f}% {float(r["buy_r20"]):>10.2f}%')
    print(f'  {"观望(<60)":>12} {float(r["avoid_r5"]):>10.2f}% {float(r["avoid_r10"]):>10.2f}% {float(r["avoid_r20"]):>10.2f}%')
    
    # 评分分档
    cur.execute("""
        SELECT 
            CASE WHEN tide_score>=80 THEN 'A:80+'
                 WHEN tide_score>=60 THEN 'B:60-79'
                 WHEN tide_score>=40 THEN 'C:40-59'
                 ELSE 'D:<40'
            END bucket,
            COUNT(*) cnt,
            AVG(future_return_5d) r5,
            AVG(future_return_10d) r10,
            AVG(future_return_20d) r20
        FROM tide_score_signal
        WHERE future_return_5d IS NOT NULL
        GROUP BY bucket ORDER BY bucket
    """)
    print(f'\n📊 评分分档收益:')
    print(f'  {"分档":>8} {"次数":>6} {"R5%":>8} {"R10%":>8} {"R20%":>8}')
    print('  ' + '-'*44)
    for r in cur.fetchall():
        print(f'  {r["bucket"]:>8} {r["cnt"]:>6} {float(r["r5"]):>8.2f} {float(r["r10"]):>8.2f} {float(r["r20"]):>8.2f}')
    
    # 汇总表写入 tide_backtest_result
    cur.execute("SELECT MAX(trade_date) FROM tide_score_signal")
    last_date = str(cur.fetchone()['MAX(trade_date)'])
    run_id = f'hist_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    
    # 清空旧的运行结果
    cur.execute("DELETE FROM tide_backtest_result WHERE run_id LIKE 'hist_%'")
    
    # 写入买入信号记录
    cur.execute("""
        INSERT INTO tide_backtest_result(run_id, ts_code, trade_date, tide_score, future_return_5d, future_return_10d, future_return_20d)
        SELECT %s, ts_code, trade_date, tide_score, future_return_5d, future_return_10d, future_return_20d
        FROM tide_score_signal
        WHERE tide_score >= 60 AND future_return_5d IS NOT NULL
    """, (run_id,))
    inserted = cur.rowcount
    conn.commit()
    print(f'\n已写入 {inserted} 条买入信号到 tide_backtest_result (run_id={run_id})')
    
    cur.close(); conn.close()
    return run_id

if __name__ == '__main__':
    compute_future_returns()
