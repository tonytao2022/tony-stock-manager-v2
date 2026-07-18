#!/usr/bin/env python3
"""
tide_backtest.py - Tide回测引擎

对历史日期列表运行Tide评分，追踪未来收益，评估策略表现
写入 tide_backtest_result 表
"""
import os, sys, json, math, logging, uuid
from datetime import date, timedelta, datetime
from typing import Dict, List, Optional, Tuple

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('tide_backtest')


def _get_trade_dates(limit: int = 60) -> List[str]:
    """获取有K线数据的交易日列表（倒序）"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline 
        WHERE is_valid=1 AND trade_date >= '2025-01-01'
        ORDER BY trade_date DESC LIMIT %s
    """, (limit,))
    dates = [str(r['trade_date']) for r in cur.fetchall()]
    cur.close(); conn.close()
    return dates


def _get_backtest_codes() -> List[str]:
    """获取回测股票列表（监控池活跃票）"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT wp.ts_code FROM watch_pool wp
        WHERE wp.is_active=1
    """)
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    if len(codes) < 50:
        # 扩充
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ts_code FROM daily_kline 
            WHERE trade_date >= '2025-06-01' AND is_valid=1
            LIMIT 500
        """)
        codes = list(set(r['ts_code'] for r in cur.fetchall()))
        cur.close(); conn.close()
    return codes


def _compute_future_returns(ts_code: str, trade_date: str, periods: List[int] = None) -> Dict:
    """计算未来N日收益"""
    if periods is None:
        periods = [5, 10, 20]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT close FROM daily_kline
        WHERE ts_code=%s AND trade_date >= %s AND is_valid=1
        ORDER BY trade_date ASC LIMIT %s
    """, (ts_code, trade_date, max(periods) + 1))
    rows = [float(r['close']) for r in cur.fetchall()]
    cur.close(); conn.close()
    if len(rows) < 2:
        return None
    entry = rows[0]
    if entry == 0:
        return None
    result = {}
    for d in periods:
        if len(rows) > d:
            result[f'future_return_{d}d'] = round((rows[d] - entry) / entry, 4)
        else:
            result[f'future_return_{d}d'] = None
    return result


def _get_tide_score_or_db(ts_code: str, trade_date: str) -> Optional[Dict]:
    """从已有数据读取Tide评分，没有则计算"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT tide_score, tide_label FROM tide_score_signal
        WHERE ts_code=%s AND trade_date=%s
    """, (ts_code, trade_date))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        return {'tide_score': float(row['tide_score']), 'tide_label': row['tide_label']}

    # 没有则现场计算
    try:
        from tide_engine.tide_scorer import run_scoring
        # 但 run_scoring 是全量入口，这里我们直接调 scorer 依赖
        import importlib
        scorer = importlib.import_module('tide_engine.tide_scorer')
        # 我们手动跑单只
        from tide_engine.tide_ic_validate import _get_tide_score
        score, l3, track, label, season = _get_tide_score(ts_code, trade_date)
        return {'tide_score': score, 'tide_label': label, 'l3_score': l3, 'track': track, 'season': season}
    except Exception as e:
        logger.debug(f'  {ts_code} {trade_date} 评分失败: {e}')
        return None


def run_backtest(lookback_days: int = 60, max_codes: int = None) -> Dict:
    """
    回测主流程
    
    对过去 N 个交易日运行:
      1. 每日对每只股票获取Tide评分
      2. 买入决策: tide_score >= 60 (买入线)
      3. 追踪未来5/10/20日收益
      4. 统计盈亏比、胜率
    
    写入 tide_backtest_result 表
    """
    run_id = f"tide_bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    trade_dates = _get_trade_dates(lookback_days)
    codes = _get_backtest_codes()
    if max_codes and len(codes) > max_codes:
        codes = codes[:max_codes]

    logger.info(f'[Tide回测] run_id={run_id}, 日期={len(trade_dates)}个, 股票={len(codes)}只')
    
    total_records = 0
    buy_signals = 0  # tide_score >= 60的买入信号
    
    conn = get_connection()
    cur = conn.cursor()
    
    for td in trade_dates:
        batch = []
        for code in codes:
            score_data = _get_tide_score_or_db(code, td)
            if score_data is None:
                continue
            ts = score_data['tide_score']
            label = score_data['tide_label']
            
            # 计算未来收益
            future = _compute_future_returns(code, td, [5, 10, 20])
            if future is None:
                continue
            
            if ts >= 60:
                buy_signals += 1
            
            batch.append((run_id, td, code, ts, label,
                          future.get('future_return_5d'),
                          future.get('future_return_10d'),
                          future.get('future_return_20d')))
        
        if batch:
            cur.executemany("""
                INSERT INTO tide_backtest_result 
                    (run_id, trade_date, ts_code, tide_score, tide_label,
                     future_return_5d, future_return_10d, future_return_20d)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, batch)
            total_records += len(batch)
        
        if len(trade_dates) > 10 and trade_dates.index(td) % 10 == 9:
            logger.info(f'  进度 {trade_dates.index(td)+1}/{len(trade_dates)}')
    
    conn.commit()
    cur.close(); conn.close()
    
    # 统计
    result = _analyze_backtest(run_id, total_records, buy_signals, trade_dates, codes)
    return result


def _analyze_backtest(run_id: str, total_records: int, buy_signals: int, 
                       trade_dates: List[str], codes: List[str]) -> Dict:
    """分析回测结果"""
    conn = get_connection()
    cur = conn.cursor()
    
    # 获取所有买入信号(tide_score >= 60)的未来收益
    cur.execute("""
        SELECT tide_score, future_return_5d, future_return_10d, future_return_20d
        FROM tide_backtest_result WHERE run_id=%s AND tide_score >= 60
    """, (run_id,))
    buys = cur.fetchall()
    cur.close(); conn.close()
    
    stats = {}
    for period in ['5d', '10d', '20d']:
        col = f'future_return_{period}'
        returns = [float(r[col]) for r in buys if r[col] is not None]
        if returns:
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r < 0]
            total_pnl = sum(returns)
            win_rate = len(wins) / len(returns) if returns else 0
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0
            profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) < 0 else 999
            avg_return = sum(returns) / len(returns)
            
            stats[period] = {
                'total_trades': len(returns),
                'wins': len(wins),
                'losses': len(losses),
                'win_rate': round(win_rate * 100, 2),
                'avg_return': round(avg_return * 100, 2),
                'total_pnl': round(total_pnl * 100, 2),
                'avg_win': round(avg_win * 100, 2),
                'avg_loss': round(avg_loss * 100, 2),
                'profit_factor': round(profit_factor, 2),
            }
    
    result = {
        'run_id': run_id,
        'trade_dates': len(trade_dates),
        'stock_count': len(codes),
        'total_records': total_records,
        'buy_signals': buy_signals,
        'buy_signal_rate': round(buy_signals / total_records * 100, 2) if total_records > 0 else 0,
        'stats': stats
    }
    
    logger.info('=' * 50)
    logger.info(f'Tide回测结果 [run_id={run_id}]')
    logger.info(f'  交易日期: {len(trade_dates)}个, 股票: {len(codes)}只')
    logger.info(f'  买入信号: {buy_signals}/{total_records} ({result["buy_signal_rate"]}%)')
    for period, s in stats.items():
        logger.info(f'  {period}: 胜率={s["win_rate"]}% 均收益={s["avg_return"]}% '
                    f'盈亏比={s["profit_factor"]} 交易={s["total_trades"]}笔')
    logger.info('=' * 50)
    
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Tide回测引擎')
    parser.add_argument('--days', type=int, default=60, help='回测天数')
    parser.add_argument('--max-stocks', type=int, default=None, help='最大股票数')
    args = parser.parse_args()
    
    result = run_backtest(lookback_days=args.days, max_codes=args.max_stocks)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
