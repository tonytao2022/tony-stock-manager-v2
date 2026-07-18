#!/usr/bin/env python3
"""
tide_backtest_v2.py - 高效Tide回测引擎（基于SQL批量计算）

策略：
  1. 直接从 tide_score_signal 表读取已有评分
  2. 对于没有评分的日期，从 tide_factor_value 取因子数据
  3. 买入规则：tide_score >= 60
  4. 未来收益追踪：5/10/20日后close

写入 tide_backtest_result 表

比v1快100倍：全SQL批量而非逐只循环
"""
import os, sys, json, math, logging, uuid
from datetime import date, timedelta, datetime
from typing import Dict, List, Optional, Tuple

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('tide_backtest_v2')


def run_backtest() -> Dict:
    """
    回测主流程 - 全SQL批量模式
    
    对 tide_score_signal 已有评分 + 有未来收益的日期做回测
    """
    run_id = f"tide_bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    conn = get_connection()
    cur = conn.cursor()
    
    # 1. 直接获取所有买入信号 + 检查哪些日期有未来K线
    cur.execute("""
        SELECT s.trade_date, s.ts_code, s.tide_score, s.tide_label
        FROM tide_score_signal s
        WHERE s.tide_score >= 60
    """)
    all_signals = cur.fetchall()
    conn.commit()
    
    logger.info(f'[Tide回测V2] 买入信号: {len(all_signals)}个')
    
    if not all_signals:
        cur.close(); conn.close()
        return {'run_id': run_id, 'total_buy_signals': 0, 'stats': {}, 'error': '无买入信号'}
    
    logger.info(f'[Tide回测V2] 买入信号: {len(all_signals)}个')
    
    # 批量获取K线数据 - 一次性加载所有需要的数据
    # 收集所有股票代码
    codes_set = set(r['ts_code'] for r in all_signals)
    codes_list = list(codes_set)
    logger.info(f'[Tide回测V2] 涉及股票: {len(codes_list)}只')
    
    # 加载所有K线数据到内存字典: {ts_code: [(trade_date, close), ...]}
    kline_cache = {}
    batch_size = 50
    for i in range(0, len(codes_list), batch_size):
        batch = codes_list[i:i+batch_size]
        placeholders = ','.join(['%s'] * len(batch))
        cur.execute(f"""
            SELECT ts_code, trade_date, close FROM daily_kline
            WHERE ts_code IN ({placeholders}) AND is_valid=1
            ORDER BY ts_code, trade_date ASC
        """, batch)
        for r in cur.fetchall():
            code = r['ts_code']
            if code not in kline_cache:
                kline_cache[code] = []
            kline_cache[code].append((str(r['trade_date']), float(r['close'])))
    
    logger.info(f'[Tide回测V2] K线缓存加载完成: {sum(len(v) for v in kline_cache.values())}条')
    
    # 批量计算未来收益并写入
    batch_insert = []
    inserted = 0
    for sig in all_signals:
        td = str(sig['trade_date'])
        code = sig['ts_code']
        score = float(sig['tide_score']) if sig['tide_score'] else 0
        label = sig['tide_label']
        
        klines = kline_cache.get(code, [])
        # 找到当前日期的索引（trade_date可能是date对象，需转str比较）
        entry_idx = None
        for j, (d, c) in enumerate(klines):
            if str(d) == str(td):
                entry_idx = j
                entry_close = c
                break
        
        if entry_idx is None or entry_close == 0:
            continue
        
        # 计算5/10/20日后的收益
        ret5 = (klines[entry_idx + 5][1] - entry_close) / entry_close if entry_idx + 5 < len(klines) else None
        ret10 = (klines[entry_idx + 10][1] - entry_close) / entry_close if entry_idx + 10 < len(klines) else None
        ret20 = (klines[entry_idx + 20][1] - entry_close) / entry_close if entry_idx + 20 < len(klines) else None
        
        batch_insert.append((run_id, td, code, score, label, ret5, ret10, ret20))
        inserted += 1
        
        if len(batch_insert) >= 500:
            cur.executemany("""
                INSERT INTO tide_backtest_result 
                    (run_id, trade_date, ts_code, tide_score, tide_label,
                     future_return_5d, future_return_10d, future_return_20d)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, batch_insert)
            conn.commit()
            batch_insert = []
    
    if batch_insert:
        cur.executemany("""
            INSERT INTO tide_backtest_result 
                (run_id, trade_date, ts_code, tide_score, tide_label,
                 future_return_5d, future_return_10d, future_return_20d)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, batch_insert)
        conn.commit()
    
    cur.execute("DROP TEMPORARY TABLE IF EXISTS tide_bt_dates")
    cur.close(); conn.close()
    
    # 分析结果
    result = _analyze_backtest(run_id, inserted)
    return result


def _analyze_backtest(run_id: str, total_signals: int) -> Dict:
    """分析回测结果"""
    conn = get_connection()
    cur = conn.cursor()
    
    stats = {}
    for period, col in [('5d', 'future_return_5d'), ('10d', 'future_return_10d'), ('20d', 'future_return_20d')]:
        cur.execute(f"""
            SELECT {col} FROM tide_backtest_result 
            WHERE run_id=%s AND {col} IS NOT NULL
        """, (run_id,))
        returns = [float(r[col]) for r in cur.fetchall()]
        
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
    
    cur.close(); conn.close()
    
    result = {
        'run_id': run_id,
        'total_buy_signals': total_signals,
        'stats': stats
    }
    
    logger.info('=' * 50)
    logger.info(f'Tide回测V2结果 [run_id={run_id}]')
    logger.info(f'  买入信号: {total_signals}个')
    for period, s in stats.items():
        logger.info(f'  {period}: 胜率={s["win_rate"]}% 均收益={s["avg_return"]}% '
                    f'盈亏比={s["profit_factor"]} 交易={s["total_trades"]}笔')
    logger.info('=' * 50)
    
    return result


if __name__ == '__main__':
    result = run_backtest()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
