"""
backtest_engine.py - 回测引擎
基于strategy_signal历史评分的模拟交易回测
"""
import logging
from datetime import date, timedelta
from db_config import db_cursor, serialize_rows
import json
logger = logging.getLogger('backtest_engine')


def run_backtest(strategy='p6_score', start_date=None, end_date=None,
                 min_score=75, max_hold=30, stop_loss=-8, pool_only=True):
    """
    执行回测
    策略: 评分≥min_score买入, 持有max_hold日或触发止损卖出
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)

    logger.info(f'[Backtest] 开始回测 {start_date}~{end_date} strategy={strategy}')

    # 获取回测股票池（所有有评分的股票）
    with db_cursor(commit=False) as cur:
        if pool_only:
            cur.execute("""
                SELECT ss.ts_code, sb.name, sb.industry
                FROM strategy_signal ss
                JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                WHERE ss.ts_code IN (SELECT ts_code FROM watch_pool WHERE is_active=1)
                GROUP BY ss.ts_code
            """)
        else:
            cur.execute("""
                SELECT ss.ts_code, sb.name, sb.industry
                FROM strategy_signal ss
                JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                GROUP BY ss.ts_code
            """)
        stocks = cur.fetchall()

    if not stocks:
        logger.warning('[Backtest] 无股票数据')
        return None

    trades = []
    total_trades = 0
    win_trades = 0
    lose_trades = 0
    total_return = 0

    for idx, stock in enumerate(stocks):
        code = stock['ts_code']
        if idx % 50 == 0:
            logger.info(f'[Backtest] 进度: {idx}/{len(stocks)}')
        result = _simulate_trade(code, start_date, end_date, min_score, max_hold, stop_loss)
        if result and result['trades']:
            trades.extend(result['trades'])
            total_trades += result['total']
            win_trades += result['wins']
            lose_trades += result['losses']
            total_return += result['total_return']

    # 汇总
    win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0

    # 计算盈亏比
    win_returns = [t['profit_pct'] for t in trades if t['profit_pct'] > 0]
    lose_returns = [t['profit_pct'] for t in trades if t['profit_pct'] <= 0]
    avg_win = sum(win_returns) / len(win_returns) if win_returns else 0
    avg_lose = abs(sum(lose_returns) / len(lose_returns)) if lose_returns else 1
    profit_factor = avg_win / avg_lose if avg_lose > 0 else 0

    avg_hold = sum(t['hold_days'] for t in trades) / len(trades) if trades else 0

    # 最大回撤（简化计算）
    max_drawdown = _calc_max_drawdown(trades)

    report = {
        'strategy': strategy,
        'period': f'{start_date}~{end_date}',
        'total_stocks': len(stocks),
        'total_trades': total_trades,
        'win_trades': win_trades,
        'lose_trades': lose_trades,
        'win_rate': round(win_rate, 2),
        'avg_win_pct': round(avg_win, 2),
        'avg_lose_pct': round(avg_lose, 2),
        'profit_factor': round(profit_factor, 2),
        'max_drawdown': round(max_drawdown, 2),
        'total_return': round(total_return, 2),
        'avg_hold_days': round(avg_hold, 1),
        'trade_count': len(trades),
    }

    # 写入数据库
    _save_report(report, trades, end_date)

    logger.info(f'[Backtest] 完成: {total_trades}笔交易, 胜率{win_rate:.1f}%, 盈亏比{profit_factor:.2f}')
    return report


def _simulate_trade(ts_code, start_date, end_date, min_score, max_hold, stop_loss):
    """对单只股票模拟交易"""
    with db_cursor(commit=False) as cur:
        # 从K线数据获取交易日序列
        # 对end_date做日期加法（兼容字符串和date对象）
        if isinstance(end_date, str):
            from datetime import datetime
            _end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        else:
            _end_date = end_date
        _end_extended = _end_date + timedelta(days=max_hold + 10)
        cur.execute("""
            SELECT trade_date, close
            FROM daily_kline
            WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date ASC
        """, [ts_code, start_date, _end_extended])
        klines_list = cur.fetchall()
        
        if len(klines_list) < 20:
            return None
        
        kline_map = {str(k['trade_date']): float(k['close']) for k in klines_list}
        trade_dates = list(kline_map.keys())

    trades = []
    holding = None
    
    # 从K线数据直接计算评分（不依赖评分引擎，快100倍）
    # 用20日收盘均线 + 10日涨跌幅作为评分依据
    step = max(1, len(trade_dates) // 60)
    
    for idx in range(step, len(trade_dates), step):
        date_str = trade_dates[idx]
        from datetime import datetime as _dt
        trade_date = _dt.strptime(str(date_str)[:10], '%Y-%m-%d').date()
        
        # 读取前20天的价格
        start_idx = max(0, idx - 20)
        window_dates = trade_dates[start_idx:idx+1]
        window_prices = [kline_map.get(d, 0) for d in window_dates]
        window_prices = [p for p in window_prices if p > 0]
        
        if len(window_prices) < 10:
            continue
        
        # 均线计算
        ma5 = sum(window_prices[-5:]) / 5 if len(window_prices) >= 5 else 0
        ma10 = sum(window_prices[-10:]) / 10 if len(window_prices) >= 10 else 0
        ma20 = sum(window_prices) / len(window_prices)
        
        # 涨跌幅
        if len(window_prices) >= 10:
            chg_10d = (window_prices[-1] - window_prices[-10]) / window_prices[-10] * 100
        else:
            chg_10d = 0
        
        latest_price = window_prices[-1]
        
        # 评分（简化版，模拟p6_scorer逻辑）
        score = 0
        trend_score = 0
        momentum_score = 0
        
        # 趋势分 (0-40)
        if ma5 > ma10 > ma20 and latest_price > ma5:
            trend_score = 40
            score += 40
        elif ma5 > ma10 and latest_price > ma10:
            trend_score = 30
            score += 30
        elif latest_price > ma20:
            trend_score = 20
            score += 20
        else:
            trend_score = 10
            score += 10
        
        # 动量分 (0-40)
        if chg_10d > 15:
            momentum_score = 40
            score += 40
        elif chg_10d > 10:
            momentum_score = 30
            score += 30
        elif chg_10d > 5:
            momentum_score = 20
            score += 20
        elif chg_10d > 0:
            momentum_score = 10
            score += 10
        else:
            momentum_score = 5
            score += 5
        
        # 波动率分 (0-20)
        high = max(window_prices[-10:])
        low = min(window_prices[-10:])
        if low > 0:
            volatility = (high - low) / low * 100
            if 5 <= volatility <= 15:
                score += 15
            elif volatility < 5:
                score += 10
            else:
                score += 5
        
        score = min(score, 100)

        # 不在持仓中：评分达标则买入
        if holding is None and score >= min_score:
            buy_price = kline_map.get(date_str, 0)
            if buy_price > 0:
                holding = {
                    'buy_date': trade_date,
                    'buy_price': buy_price,
                    'entry_index': idx,
                }

        # 在持仓中：检查卖出条件
        if holding is not None:
            hold_days = (trade_date - holding['buy_date']).days
            current_price = kline_map.get(date_str, 0)

            sell_signal = False
            sell_reason = ''
            sell_price = current_price

            # 条件1: 达到最长持有日
            if hold_days >= max_hold:
                sell_signal = True
                sell_reason = 'max_hold'

            # 条件2: 触发止损
            if current_price > 0 and holding['buy_price'] > 0:
                profit = (current_price - holding['buy_price']) / holding['buy_price'] * 100
                if profit <= stop_loss:
                    sell_signal = True
                    sell_reason = 'stop_loss'

            # 条件3: 评分下降卖出
            if not sell_signal and hold_days >= 5:
                if score < min_score - 5:
                    sell_signal = True
                    sell_reason = 'score_decline'

            if sell_signal and current_price > 0 and holding['buy_price'] > 0:
                profit_pct = (current_price - holding['buy_price']) / holding['buy_price'] * 100
                trades.append({
                    'ts_code': ts_code,
                    'buy_date': str(holding['buy_date']),
                    'sell_date': date_str,
                    'hold_days': hold_days,
                    'buy_price': round(holding['buy_price'], 2),
                    'sell_price': round(sell_price, 2),
                    'profit_pct': round(profit_pct, 2),
                    'exit_reason': sell_reason,
                    'signal_type': '',
                    'season': '',
                })
                holding = None

    # 最后一只处理: 如果还在持仓，按最后价格强制平仓
    if holding is not None and trade_dates:
        last_date_str = trade_dates[-1]
        last_price = kline_map.get(last_date_str, 0)
        if last_price > 0 and holding['buy_price'] > 0:
            hold_days = (date.fromisoformat(last_date_str) - holding['buy_date']).days if isinstance(last_date_str, str) else 0
            profit_pct = (last_price - holding['buy_price']) / holding['buy_price'] * 100
            trades.append({
                'ts_code': ts_code,
                'buy_date': str(holding['buy_date']),
                'sell_date': last_date_str,
                'hold_days': hold_days,
                'buy_price': round(holding['buy_price'], 2),
                'sell_price': round(last_price, 2),
                'profit_pct': round(profit_pct, 2),
                'exit_reason': 'force_close',
                'signal_type': '',
                'season': '',
            })

    wins = sum(1 for t in trades if t['profit_pct'] > 0)
    losses = sum(1 for t in trades if t['profit_pct'] <= 0)
    total_ret = sum(t['profit_pct'] for t in trades)

    return {'trades': trades, 'total': len(trades), 'wins': wins,
            'losses': losses, 'total_return': total_ret}


def _calc_max_drawdown(trades):
    """计算最大回撤"""
    if not trades:
        return 0
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t['profit_pct']
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _save_report(report, trades, report_date):
    """保存回测报告到数据库"""
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO backtest_report
                    (report_date, strategy_name, total_trades, win_trades,
                     lose_trades, win_rate, avg_win_pct, avg_lose_pct,
                     profit_factor, max_drawdown, total_return, avg_hold_days,
                     trade_records)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                report_date,
                report['strategy'],
                report['total_trades'],
                report['win_trades'],
                report['lose_trades'],
                report['win_rate'],
                report['avg_win_pct'],
                report['avg_lose_pct'],
                report['profit_factor'],
                report['max_drawdown'],
                report['total_return'],
                report['avg_hold_days'],
                json.dumps(trades[:100], ensure_ascii=False, default=str),
            ))
            report_id = cur.lastrowid

            # 保存交易明细（仅前200条）
            for t in trades[:200]:
                cur.execute("""
                    INSERT INTO backtest_trade_detail
                        (report_id, ts_code, name, buy_date, sell_date,
                         hold_days, buy_price, sell_price, profit_pct,
                         direction, exit_reason, signal_type, season)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    report_id,
                    t.get('ts_code', ''),
                    '',
                    t.get('buy_date', ''),
                    t.get('sell_date', ''),
                    t.get('hold_days', 0),
                    t.get('buy_price', 0),
                    t.get('sell_price', 0),
                    t.get('profit_pct', 0),
                    'LONG',
                    t.get('exit_reason', ''),
                    t.get('signal_type', ''),
                    t.get('season', ''),
                ))
    except Exception as e:
        logger.error(f'[Backtest] 保存报告失败: {e}')


def list_reports(limit=10):
    """获取历史回测报告列表"""
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT id, report_date, strategy_name, total_trades, win_rate,
                   profit_factor, total_return, max_drawdown, created_at
            FROM backtest_report
            ORDER BY id DESC LIMIT %s
        """, [limit])
        return serialize_rows(cur.fetchall())


def get_report_detail(report_id):
    """获取回测报告详情"""
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM backtest_report WHERE id=%s", [report_id])
        report = cur.fetchone()
        if not report:
            return None

        cur.execute("""
            SELECT * FROM backtest_trade_detail WHERE report_id=%s
            ORDER BY profit_pct DESC LIMIT 200
        """, [report_id])
        trades = cur.fetchall()

    result = serialize_rows([report])[0]
    result['trades'] = serialize_rows(trades)
    return result
