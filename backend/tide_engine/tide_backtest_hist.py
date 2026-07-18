#!/usr/bin/env python3
"""
Tide历史回测：从2026-06-01到2026-07-03的24个交易日，
每天计算一次Tide评分，追踪未来5/10/20日收益。
"""
import sys, os, time, traceback
from datetime import datetime, timedelta, date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_config import get_connection

def get_trade_dates(start_d, end_d):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline 
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (start_d, end_d))
    dates = [str(r['trade_date']) for r in cur.fetchall()]
    cur.close(); conn.close()
    return dates

def run_scoring_for_date(trade_date):
    """对某个交易日执行Tide评分"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM tide_score_signal WHERE trade_date=%s", (trade_date,))
    existing = cur.fetchone()['cnt']
    cur.close(); conn.close()
    if existing > 0:
        print(f'  [{trade_date}] 已有{existing}条评分，跳过')
        return True
    
    # 用 tide_scorer 跑当天
    from tide_engine.tide_scorer import run_scoring
    try:
        result = run_scoring(date.fromisoformat(trade_date))
        ok = result.get('total', result.get('success', 0))
        print(f'  [{trade_date}] Tide评分完成: {ok}条')
        return True
    except Exception as e:
        print(f'  [{trade_date}] ❌ 评分失败: {e}')
        traceback.print_exc()
        return False

def compute_future_returns():
    """计算所有tide_score_signal的未来收益"""
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT s.id, s.ts_code, s.trade_date, s.tide_score
        FROM tide_score_signal s
        ORDER BY s.trade_date, s.ts_code
    """)
    signals = cur.fetchall()
    print(f'共{len(signals)}条评分记录，计算未来收益...')
    
    # 取K线
    cur.execute("""
        SELECT ts_code, trade_date, close 
        FROM daily_kline
        ORDER BY ts_code, trade_date
    """)
    kline_rows = cur.fetchall()
    
    kline_map = {}
    for r in kline_rows:
        code = r['ts_code']
        if code not in kline_map:
            kline_map[code] = []
        kline_map[code].append((str(r['trade_date']), float(r['close'])))
    
    updated = 0
    for sig in signals:
        sig_id = sig['id']
        code = sig['ts_code']
        sig_date = str(sig['trade_date'])
        
        if code not in kline_map:
            continue
        
        dates_arr = kline_map[code]
        idx = next((i for i, (d, _) in enumerate(dates_arr) if d == sig_date), None)
        if idx is None:
            continue
        
        base_close = dates_arr[idx][1]
        
        def get_ret(steps):
            tgt = idx + steps
            if tgt < len(dates_arr):
                return (dates_arr[tgt][1] - base_close) / base_close * 100
            return None
        
        ret5 = get_ret(5)
        ret10 = get_ret(10)
        ret20 = get_ret(20)
        
        cur.execute("""
            UPDATE tide_score_signal 
            SET future_return_5d=%s, future_return_10d=%s, future_return_20d=%s
            WHERE id=%s
        """, (ret5, ret10, ret20, sig_id))
        updated += 1
    
    conn.commit()
    print(f'已更新{updated}条记录的未来收益')
    cur.close(); conn.close()
    return updated

def print_summary():
    conn = get_connection()
    cur = conn.cursor()
    
    # 每日汇总
    cur.execute("""
        SELECT trade_date, COUNT(*) cnt, AVG(tide_score) avg_score,
               AVG(future_return_5d) r5, AVG(future_return_10d) r10, AVG(future_return_20d) r20
        FROM tide_score_signal
        WHERE tide_score IS NOT NULL
        GROUP BY trade_date ORDER BY trade_date
    """)
    print(f'{"日期":>12} {"数量":>5} {"均值":>6} {"R5%":>8} {"R10%":>8} {"R20%":>8}')
    print('-' * 55)
    for r in cur.fetchall():
        r5 = f'{float(r["r5"]):.2f}' if r['r5'] else 'N/A'
        r10 = f'{float(r["r10"]):.2f}' if r['r10'] else 'N/A'
        r20 = f'{float(r["r20"]):.2f}' if r['r20'] else 'N/A'
        print(f'{str(r["trade_date"]):>12} {r["cnt"]:>5} {float(r["avg_score"]):>6.1f} {r5:>8} {r10:>8} {r20:>8}')
    
    # 评分分档
    cur.execute("""
        SELECT 
            CASE 
                WHEN tide_score >= 80 THEN 'A:80+'
                WHEN tide_score >= 60 THEN 'B:60-79'
                WHEN tide_score >= 40 THEN 'C:40-59'
                ELSE 'D:<40'
            END AS bucket,
            COUNT(*) cnt,
            AVG(future_return_5d) r5, AVG(future_return_10d) r10, AVG(future_return_20d) r20
        FROM tide_score_signal
        WHERE future_return_5d IS NOT NULL
        GROUP BY bucket ORDER BY bucket
    """)
    print(f'\n📊 按评分分档(含全时期):')
    print(f'{"分档":>10} {"次数":>6} {"R5%":>8} {"R10%":>8} {"R20%":>8}')
    print('-' * 50)
    for r in cur.fetchall():
        print(f'{r["bucket"]:>10} {r["cnt"]:>6} {float(r["r5"]):>8.2f} {float(r["r10"]):>8.2f} {float(r["r20"]):>8.2f}')
    
    # 买入信号绩效(≥60)
    cur.execute("""
        SELECT COUNT(*) cnt,
               AVG(future_return_5d) r5, AVG(future_return_10d) r10, AVG(future_return_20d) r20,
               SUM(CASE WHEN future_return_20d > 0 THEN 1 ELSE 0 END) AS win20
        FROM tide_score_signal
        WHERE tide_score >= 60 AND future_return_20d IS NOT NULL
    """)
    r = cur.fetchone()
    print(f'\n📊 Tide买入信号(≥60分) 20日绩效:')
    print(f'  买入次数: {r["cnt"]}')
    print(f'  5日平均: {float(r["r5"]):.2f}%')
    print(f'  10日平均: {float(r["r10"]):.2f}%')
    print(f'  20日平均: {float(r["r20"]):.2f}%')
    print(f'  20日胜率: {float(r["win20"])/r["cnt"]*100:.1f}%')
    
    # 买入信号当前收益(不限制20日必须有)
    cur.execute("""
        SELECT COUNT(*) cnt, AVG(future_return_5d) r5, AVG(future_return_10d) r10
        FROM tide_score_signal
        WHERE tide_score >= 60 AND future_return_5d IS NOT NULL
    """)
    r = cur.fetchone()
    print(f'\n📊 Tide买入信号(≥60分) 短期绩效:')
    print(f'  5日平均: {float(r["r5"]):.2f}% (共{r["cnt"]}次)')
    
    cur.close(); conn.close()

def main():
    start_date = '2026-06-01'
    end_date = '2026-07-03'
    
    print(f'===== Tide历史回测: {start_date} ~ {end_date} =====')
    
    trade_dates = get_trade_dates(start_date, end_date)
    print(f'交易日数: {len(trade_dates)} [{trade_dates[0]} ~ {trade_dates[-1]}]')
    
    # 逐日评分
    print('\n--- 第1步: 逐日评分 ---')
    ok = 0
    fail = 0
    for td in trade_dates:
        time.sleep(0.2)
        if run_scoring_for_date(td):
            ok += 1
        else:
            fail += 1
    print(f'评分: 成功{ok}, 失败{fail}')
    
    # 计算未来收益
    print('\n--- 第2步: 计算未来收益 ---')
    compute_future_returns()
    
    # 汇总
    print('\n--- 第3步: 汇总 ---')
    print_summary()
    print('\n✅ 历史回测完成')

if __name__ == '__main__':
    main()
