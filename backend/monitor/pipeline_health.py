"""
pipeline_health.py — 管道健康监控
4维度: 惩罚分有效性 / 评分分布漂移 / 季节置信度 / 静默失效检测

每个维度输出: {status: 'OK'|'WARN'|'CRIT', score: 0-100, message: str, detail: dict}
"""
import sys, os, json
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from db_config import get_connection


def check_penalty_effectiveness(days=5, threshold=0.5):
    """
    维度1: 惩罚分有效性
    检测: penalty_log 是否有数据? 平均分>阈值? 零值占比?
    """
    conn = get_connection()
    cur = conn.cursor()
    from_dt = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    cur.execute("SELECT COUNT(*) as cnt FROM penalty_log WHERE trade_date >= %s", (from_dt,))
    total = cur.fetchone()['cnt']
    
    cur.execute("""
        SELECT trade_date, AVG(penalty_points) as avg_pts, COUNT(*) as cnt
        FROM penalty_log WHERE trade_date >= %s
        GROUP BY trade_date ORDER BY trade_date DESC
    """, (from_dt,))
    daily_rows = cur.fetchall()
    
    zero_count = sum(1 for r in daily_rows if (r['avg_pts'] or 0) < 0.01)
    
    cur.close(); conn.close()
    
    if total == 0:
        return {
            'status': 'CRIT',
            'score': 0,
            'message': f'最近{days}天penalty_log完全为空 — 惩罚分未生效',
            'detail': {'total_records': 0, 'days_checked': days, 'days_with_data': 0}
        }
    
    if not daily_rows:
        return {'status': 'WARN', 'score': 30, 'message': '无足够数据评估', 'detail': {}}
    
    avg_all = sum(r['avg_pts'] or 0 for r in daily_rows) / len(daily_rows)
    zero_ratio = zero_count / len(daily_rows)
    
    if avg_all < threshold:
        status = 'CRIT' if avg_all < threshold * 0.5 else 'WARN'
        score = int(min(100, max(0, (avg_all / threshold) * 50)))
        msg = f'平均惩罚分{avg_all:.2f} < 阈值{threshold} — 可能静默失效'
    elif zero_ratio > 0.3:
        status = 'WARN'
        score = 40
        msg = f'零值天数占比{zero_ratio:.0%} > 30% — 惩罚分覆盖不足'
    else:
        status = 'OK'
        score = int(min(100, 50 + (avg_all - threshold) * 20))
        msg = f'正常: 最近{days}天平均惩罚分{avg_all:.2f}'
    
    return {
        'status': status,
        'score': min(100, score),
        'message': msg,
        'detail': {
            'total_records': total,
            'avg_penalty': round(avg_all, 2),
            'zero_ratio': round(zero_ratio, 2),
            'daily_breakdown': [{'date': str(r['trade_date']), 'avg_pts': round(r['avg_pts'] or 0, 2),
                                  'count': r['cnt']} for r in daily_rows]
        }
    }


def check_score_distribution(days=7, drift_threshold=5):
    """
    维度2: 评分分布漂移
    检测: 当日评分均值 vs 前N日均值的偏移量
    """
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT trade_date, AVG(composite_score) as mean_score,
               AVG(calibrated_score) as mean_cal
        FROM strategy_signal
        GROUP BY trade_date ORDER BY trade_date DESC LIMIT %s
    """, (days + 1,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    
    if len(rows) < 2:
        return {'status': 'WARN', 'score': 30, 'message': f'数据不足{len(rows)}天', 'detail': {}}
    
    latest = rows[0]
    prev_days = rows[1:]
    
    latest_mean = float(latest['mean_score'] or 0)
    prev_mean = sum(float(r['mean_score'] or 0) for r in prev_days) / len(prev_days)
    
    drift = abs(latest_mean - prev_mean)
    
    detail = {
        'latest_date': str(latest['trade_date']),
        'latest_mean': round(latest_mean, 2),
        'prev_mean': round(prev_mean, 2),
        'drift': round(drift, 2),
        'recent_days': [{'date': str(r['trade_date']), 'mean': round(float(r['mean_score'] or 0), 2),
                          'cal_mean': round(float(r['mean_cal'] or 0), 2)} for r in rows[:7]]
    }
    
    if drift > drift_threshold * 2:
        return {
            'status': 'CRIT', 'score': int(max(0, 50 - drift * 3)),
            'message': f'评分均值偏移{drift:.1f}分 (阈值{drift_threshold}) — 严重分布漂移',
            'detail': detail
        }
    elif drift > drift_threshold:
        return {
            'status': 'WARN', 'score': int(max(0, 60 - drift * 2)),
            'message': f'评分均值偏移{drift:.1f}分 (阈值{drift_threshold}) — 分布漂移',
            'detail': detail
        }
    else:
        return {
            'status': 'OK', 'score': int(min(100, 80 - drift * 2)),
            'message': f'正常: 均值{latest_mean:.1f} 偏移{drift:.1f}分',
            'detail': detail
        }


def check_season_confidence(days=7, min_confidence=0.3, warn_days=3):
    """
    维度3: 季节判定置信度
    检测: season_state 中连续同季天数, 置信度<阈值且持续多天告警
    """
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT trade_date, season, index_code
        FROM season_state
        WHERE index_code = 'MARKET'
        ORDER BY trade_date DESC LIMIT %s
    """, (days * 2,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    
    if not rows:
        return {'status': 'WARN', 'score': 20, 'message': 'season_state 无数据', 'detail': {}}
    
    seasons = [(str(r['trade_date']), r['season']) for r in rows]
    if len(seasons) < 2:
        return {'status': 'OK', 'score': 50, 'message': '数据不足2天', 'detail': {'current_season': seasons[0][1] if seasons else 'unknown'}}
    
    consecutive = 1
    max_consecutive = 1
    current_season = seasons[0][1]
    for i in range(1, len(seasons)):
        if seasons[i][1] == seasons[i-1][1]:
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)
        else:
            consecutive = 1
    
    confidence = min(1.0, max_consecutive / 30)
    
    switches = sum(1 for i in range(1, min(warn_days, len(seasons))) if seasons[i][1] != seasons[i-1][1])
    
    if switches >= 2 and confidence < min_confidence:
        return {
            'status': 'CRIT',
            'score': 10,
            'message': f'季节"{current_season}"置信度{confidence:.2f} < {min_confidence}, 最近{switches}次切换',
            'detail': {'current_season': current_season, 'confidence': round(confidence, 2),
                       'max_consecutive_days': max_consecutive, 'switches_recent': switches,
                       'recent': [{'date': r[0], 'season': r[1]} for r in seasons[:7]]}
        }
    elif confidence < min_confidence:
        return {
            'status': 'WARN',
            'score': max(10, int(confidence * 100)),
            'message': f'季节"{current_season}"置信度偏低: {confidence:.2f}',
            'detail': {'current_season': current_season, 'confidence': round(confidence, 2),
                       'max_consecutive_days': max_consecutive, 'switches_recent': switches,
                       'recent': [{'date': r[0], 'season': r[1]} for r in seasons[:7]]}
        }
    else:
        return {
            'status': 'OK',
            'score': int(min(100, 60 + confidence * 40)),
            'message': f'季节"{current_season}"置信度{confidence:.2f}, 连续{max_consecutive}天',
            'detail': {'current_season': current_season, 'confidence': round(confidence, 2),
                       'max_consecutive_days': max_consecutive, 'switches_recent': switches}
        }


def check_silent_failures(days=7):
    """
    维度4: 静默失效检测
    检测: pipeline_exec_log 中异常标记, daily_kline 连续缺失天数
    """
    conn = get_connection()
    cur = conn.cursor()
    from_dt = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    # pipeline_exec_log 检查 (有started_at 无 trade_date 字段)
    cur.execute("""
        SELECT DATE(started_at) AS log_date, step_name, status, error_msg
        FROM pipeline_exec_log
        WHERE started_at >= %s AND status IN ('FAILED','ERROR')
        ORDER BY started_at DESC LIMIT 20
    """, (from_dt,))
    failed_steps = cur.fetchall()
    
    # daily_kline 最近是否有数据
    cur.execute("""
        SELECT trade_date FROM daily_kline 
        WHERE trade_date >= %s
        GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5
    """, (from_dt,))
    kline_dates = [str(r['trade_date']) for r in cur.fetchall()]
    
    # is_calculable = 0 的记录数
    cur.execute("""
        SELECT trade_date, COUNT(*) as cnt
        FROM strategy_signal
        WHERE trade_date >= %s AND is_calculable = 0
        GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5
    """, (from_dt,))
    uncalculable = cur.fetchall()
    
    cur.close(); conn.close()
    
    issues = []
    
    if failed_steps:
        issues.append(f'{len(failed_steps)}个管道步骤失败')
    
    if not kline_dates:
        issues.append(f'最近{days}天K线数据完全缺失')
    
    if uncalculable:
        total_uncal = sum(r['cnt'] for r in uncalculable)
        issues.append(f'{total_uncal}条评分标记为不可计算(is_calculable=0)')
    
    if not issues:
        return {
            'status': 'OK', 'score': 100,
            'message': f'最近{days}天无静默失效',
            'detail': {'kline_dates': kline_dates, 'failed_steps': 0, 'uncalculable_records': 0}
        }
    elif len(issues) >= 2:
        return {
            'status': 'CRIT', 'score': 20,
            'message': '; '.join(issues),
            'detail': {'failed_steps': [{'date': str(r.get('log_date','')), 'step': r['step_name'],
                                          'error': (r['error_msg'] or '')[:100]}
                                         for r in failed_steps],
                       'kline_dates': kline_dates,
                       'uncalculable': [{'date': str(r['trade_date']), 'count': r['cnt']} for r in uncalculable]}
        }
    else:
        return {
            'status': 'WARN', 'score': 60,
            'message': '; '.join(issues),
            'detail': {'failed_steps': len(failed_steps), 'kline_dates': kline_dates,
                       'uncalculable': uncalculable[0] if uncalculable else {}}
        }


def get_all_health(days=7):
    """聚合所有维度, 输出总健康分"""
    dims = {
        'penalty_effectiveness': check_penalty_effectiveness(days=min(days, 5)),
        'score_distribution': check_score_distribution(days=min(days, 7)),
        'season_confidence': check_season_confidence(days=min(days, 7)),
        'silent_failures': check_silent_failures(days=min(days, 7)),
    }
    
    weights = {'penalty_effectiveness': 0.25, 'score_distribution': 0.30,
               'season_confidence': 0.25, 'silent_failures': 0.20}
    
    total = 0
    for k, v in dims.items():
        total += v['score'] * weights.get(k, 0.25)
    
    status_order = {'OK': 0, 'WARN': 1, 'CRIT': 2}
    global_status = max(dims.values(), key=lambda x: status_order.get(x['status'], 0))['status']
    
    return {
        'overall_score': round(total, 1),
        'overall_status': global_status,
        'dimensions': dims,
        'timestamp': datetime.now().isoformat(),
    }


def print_report(days=7):
    """打印健康报告到控制台"""
    health = get_all_health(days=days)
    print(f"\n{'='*60}")
    print(f"🔬 管道健康报告 | 总分: {health['overall_score']}/100 | 状态: {health['overall_status']}")
    print(f"{'='*60}")
    for dim, result in health['dimensions'].items():
        icon = {'OK': '✅', 'WARN': '⚠️', 'CRIT': '🔴'}.get(result['status'], '❓')
        print(f"  {icon} [{result['status']}] {dim}: {result['message']} ({result['score']}分)")
    print(f"{'='*60}\n")
    return health


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7, help='检查天数范围')
    parser.add_argument('--dimension', type=str, default='all',
                        choices=['all', 'penalty', 'score', 'season', 'silent'])
    args = parser.parse_args()
    
    if args.dimension == 'all':
        print_report(days=args.days)
    else:
        dim_map = {
            'penalty': check_penalty_effectiveness,
            'score': check_score_distribution,
            'season': check_season_confidence,
            'silent': check_silent_failures,
        }
        fn = dim_map.get(args.dimension)
        if fn:
            result = fn(days=args.days)
            print(json.dumps(result, ensure_ascii=False, indent=2))
