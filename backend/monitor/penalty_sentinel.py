"""
penalty_sentinel.py — 惩罚分有效性哨兵
检测连续5天平均惩罚分<0.5 → 输出告警
"""
import sys
from datetime import datetime, timedelta
from db_config import get_connection

def check_penalty_health(days=5, threshold=0.5, silent=False):
    """
    检查最近N天的惩罚分有效性
    
    Returns:
        (healthy: bool, report: dict)
    """
    conn = get_connection()
    cur = conn.cursor()
    
    from_dt = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    # 最近days天有多少个交易日有惩罚记录
    cur.execute("""
        SELECT trade_date, COUNT(*) as cnt, 
               AVG(penalty_points) as avg_pts, 
               COUNT(DISTINCT ts_code) as unique_stocks
        FROM penalty_log 
        WHERE trade_date >= %s
        GROUP BY trade_date 
        ORDER BY trade_date DESC
    """, (from_dt,))
    rows = cur.fetchall()
    
    cur.execute("""
        SELECT COUNT(*) as total_days 
        FROM daily_kline 
        WHERE trade_date >= %s AND trade_date <= %s
        GROUP BY trade_date
    """, (from_dt, datetime.now().strftime('%Y-%m-%d')))
    trade_days = len(cur.fetchall())
    
    cur.close()
    conn.close()
    
    if not rows:
        report = {
            'status': 'WARN',
            'message': f'最近{days}天惩罚日志为空 — 惩罚分可能未生效',
            'days_checked': days,
            'trade_days_in_range': trade_days,
            'days_with_data': 0,
            'avg_penalty': 0,
            'total_records': 0,
        }
        if not silent:
            print(f"  ⚠️ [{report['status']}] {report['message']}")
        return False, report
    
    total_records = sum(r['cnt'] for r in rows)
    avg_penalty = sum(r['avg_pts'] or 0 for r in rows) / len(rows)
    
    healthy = avg_penalty >= threshold
    status = 'OK' if healthy else 'WARN'
    
    report = {
        'status': status,
        'message': f'最近{days}天: 平均惩罚分{avg_penalty:.2f} (阈值{threshold}), {"正常" if healthy else "⚠️ 偏低"}',
        'days_checked': days,
        'trade_days_in_range': trade_days,
        'days_with_data': len(rows),
        'total_records': total_records,
        'avg_penalty': round(avg_penalty, 2),
        'daily_breakdown': [{'date': str(r['trade_date']), 'count': r['cnt'], 
                             'avg_penalty': round(r['avg_pts'] or 0, 2), 
                             'unique_stocks': r['unique_stocks']} for r in rows],
    }
    
    if not silent:
        print(f"  {'✅' if healthy else '⚠️'} [{status}] {report['message']}")
        for d in report['daily_breakdown']:
            print(f"    {d['date']}: {d['count']}条, 均分{d['avg_penalty']}, {d['unique_stocks']}只股票")
    
    return healthy, report


def get_penalty_stats(trade_date=None, ts_code=None):
    """获取某天/某股票的惩罚分统计"""
    conn = get_connection()
    cur = conn.cursor()
    
    where = []
    params = []
    if trade_date:
        where.append("trade_date = %s")
        params.append(trade_date)
    if ts_code:
        where.append("ts_code = %s")
        params.append(ts_code)
    
    where_clause = " AND ".join(where) if where else "1=1"
    
    cur.execute(f"""
        SELECT ts_code, trade_date, track, rule_name, 
               SUM(penalty_points) as total_pts,
               COUNT(*) as trigger_count
        FROM penalty_log
        WHERE {where_clause}
        GROUP BY ts_code, trade_date, track, rule_name
        ORDER BY total_pts DESC
        LIMIT 50
    """, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


if __name__ == '__main__':
    healthy, report = check_penalty_health(days=5)
    sys.exit(0 if healthy else 1)
