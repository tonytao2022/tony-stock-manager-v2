#!/usr/bin/env python3
"""
收盘后刷新涨停板数据（用daily_kline每日涨幅数据）
- limit_up_daily: 从daily_kline取涨幅>=9.5%的股票，仅存储真实行情数据
- dragon_tiger_daily: 不模拟，仅保留已有数据

为什么需要这个步骤？
打板助手依赖limit_up_daily表作为数据源，该表之前只手动拉过6月5日~15日数据，
需要每日自动补充，否则打板助手看不到当天涨停数据。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_config import get_connection

def refresh_dragon():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT MAX(trade_date) as d FROM daily_kline")
    td = cur.fetchone()['d']
    if not td:
        print("  ❌ daily_kline无数据")
        return
    td_str = str(td)

    # limit_up_daily: 取涨幅>=9.5%的股票
    cur.execute("SELECT MAX(trade_date) as d FROM limit_up_daily")
    last = cur.fetchone()['d']
    last_str = str(last) if last else ''
    if last_str >= td_str:
        print(f"  ⏭️ limit_up_daily已是最新({td_str})")
        return

    cur.execute("""
        SELECT dk.ts_code, COALESCE(sb.name, '') as name,
               dk.close, dk.change_pct, COALESCE(db.turnover_rate, 0) as turnover_rate
        FROM daily_kline dk
        LEFT JOIN stock_basic sb ON dk.ts_code = sb.ts_code
        LEFT JOIN daily_basic db ON dk.ts_code = db.ts_code AND db.trade_date = %s
        WHERE dk.trade_date = %s AND dk.change_pct >= 9.5
    """, (td_str, td_str))
    rows = cur.fetchall()

    saved = 0
    for r in rows:
        chg = float(r['change_pct'] or 0)
        tr = float(r['turnover_rate'] or 0)
        cur.execute("""
            INSERT INTO limit_up_daily (ts_code, name, trade_date, limit_up_time, change_pct, turnover_rate, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            change_pct=VALUES(change_pct), turnover_rate=VALUES(turnover_rate)
        """, (
            r['ts_code'], r['name'] or '', td_str,
            '09:30:00', round(chg, 2), round(tr, 2),
            r['name'] or ''
        ))
        saved += 1
        if saved % 50 == 0: conn.commit()
    conn.commit()
    print(f"  ✅ limit_up_daily: {saved}条 ({td_str})")
    cur.close()
    conn.close()

if __name__ == '__main__':
    refresh_dragon()
