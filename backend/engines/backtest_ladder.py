"""
backtest_ladder.py - 阶梯策略回测引擎（V2.0）
基于V1.0阶梯回测逻辑重构，完全独立
"""
import logging
import json
import statistics
from datetime import date, timedelta, datetime
from collections import defaultdict
from db_config import db_cursor, serialize_rows

logger = logging.getLogger('backtest_ladder')


# ─── 默认策略参数（V1实盘参数） ───
DEFAULT_PARAMS = {
    'buy_score': 30,         # 买入门槛
    'stop_5d_score': 20,     # 5日止损评分线
    'stop_drawdown': 10,     # 回撤止损%
    'check_10_score': 20,    # 10日检查评分
    'check_20_score': 20,    # 20日检查评分
    'check_30_score': 30,    # 30日检查评分
    'half_loss': 5,          # 减半仓亏损线%
    'half_score': 25,        # 减半仓评分线
    'max_hold': 60,          # 最长持有日
    'trailing_stop': 15,     # 移动止盈回撤%
}


def calc_simple_score(row, season=''):
    """基于单日K线的简化评分（模拟P6评分逻辑）"""
    close = float(row['close'])
    open_p = float(row['open'])
    high = float(row['high'])
    low = float(row['low'])
    change_pct = float(row['change_pct']) if 'change_pct' in row and row['change_pct'] else 0
    vol = float(row['vol']) if 'vol' in row and row['vol'] else 0

    score = 50  # 基础分

    # 涨跌幅贡献
    if change_pct > 9.5:
        score += 30
    elif change_pct > 5:
        score += 20
    elif change_pct > 2:
        score += 15
    elif change_pct > 0:
        score += 8
    elif change_pct > -2:
        score += 2
    elif change_pct > -5:
        score -= 5
    else:
        score -= 15

    # 阳线贡献
    if close > open_p:
        score += 10
    elif close < open_p:
        score -= 5

    # 振幅贡献
    amp = (high - low) / open_p * 100 if open_p > 0 else 0
    if 3 <= amp <= 8:
        score += 5
    elif amp > 8:
        score += 2

    # 成交量贡献（简化，假定vol为手）
    if vol > 0:
        vol_ratio = vol / 50000  # 粗略参考
        if vol_ratio > 3:
            score += 8
        elif vol_ratio > 1.5:
            score += 4
        elif vol_ratio < 0.3:
            score -= 3

    # 季节调整
    if season in ('spring', 'summer'):
        score += 5
    elif season in ('winter', 'panic'):
        score -= 5

    return max(0, min(100, score))


def run_ladder_backtest(params=None, pool_only=True):
    """
    阶梯策略全量回测
    返回：多策略对比结果 + 详细统计
    """
    if params is None:
        params = DEFAULT_PARAMS.copy()

    p = {**DEFAULT_PARAMS, **params}
    strategy_name = _gen_strategy_name(p)

    logger.info(f'[LadderBacktest] 开始回测 strategy={strategy_name}')

    # 获取回测股票
    with db_cursor(commit=False) as cur:
        if pool_only:
            cur.execute("""
                SELECT bp.ts_code, bp.name, bp.industry
                FROM backtest_pool bp
                WHERE bp.is_active=1
            """)
        else:
            cur.execute("""
                SELECT wp.ts_code, wp.name, wp.industry
                FROM watch_pool wp
                WHERE wp.is_active=1
            """)
        stocks = cur.fetchall()

    if not stocks:
        logger.warning('[LadderBacktest] 回测池为空')
        return None

    # 加载K线数据（先确定公共时间范围）
    # 用所有股票K线的最早/最晚交集
    all_results = {}
    multi_trades = []  # 合并所有交易

    for stock in stocks:
        code = stock['ts_code']
        try:
            result = _simulate_ladder(code, p)
            if result and result['trades']:
                multi_trades.extend(result['trades'])
                all_results[code] = result
        except Exception as e:
            logger.debug(f'[LadderBacktest] {code} 跳过: {e}')

    if not multi_trades:
        return {'strategy': strategy_name, 'trades': [], 'total_trades': 0}

    # ─── 统一统计 ───
    total_trades = len(multi_trades)
    win_trades = [t for t in multi_trades if t['profit_pct'] > 0]
    lose_trades = [t for t in multi_trades if t['profit_pct'] <= 0]
    win_count = len(win_trades)
    lose_count = len(lose_trades)
    win_rate = win_count / total_trades * 100 if total_trades > 0 else 0

    # 均盈/均亏
    avg_win = statistics.mean([t['profit_pct'] for t in win_trades]) if win_trades else 0
    avg_lose = statistics.mean([t['profit_pct'] for t in lose_trades]) if lose_trades else 0
    profit_factor = avg_win / abs(avg_lose) if avg_lose != 0 else 0

    # 总收益（累加）
    total_return = sum(t['profit_pct'] for t in multi_trades)
    # 平均持有
    avg_hold = statistics.mean([t['hold_days'] for t in multi_trades]) if multi_trades else 0

    # 持有期分段统计
    hold_segments = defaultdict(list)
    for t in multi_trades:
        seg = _hold_segment(t['hold_days'])
        hold_segments[seg].append(t['profit_pct'])

    hold_stats = {}
    for seg, profits in sorted(hold_segments.items()):
        seg_win = [p for p in profits if p > 0]
        hold_stats[seg] = {
            'count': len(profits),
            'avg_return': round(statistics.mean(profits), 2),
            'win_rate': round(len(seg_win) / len(profits) * 100, 1) if profits else 0,
        }

    # 退出原因统计
    exit_reasons = defaultdict(list)
    for t in multi_trades:
        exit_reasons[t.get('exit_reason', 'unknown')].append(t['profit_pct'])
    exit_stats = {}
    for reason, profits in exit_reasons.items():
        r_win = [p for p in profits if p > 0]
        exit_stats[reason] = {
            'count': len(profits),
            'avg_return': round(statistics.mean(profits), 2),
            'win_rate': round(len(r_win) / len(profits) * 100, 1) if profits else 0,
        }

    # 评分段统计
    score_ranges = defaultdict(list)
    for t in multi_trades:
        sr = _score_range(t.get('buy_score', 0))
        score_ranges[sr].append(t['profit_pct'])
    score_stats = {}
    for sr, profits in sorted(score_ranges.items()):
        sr_win = [p for p in profits if p > 0]
        score_stats[sr] = {
            'count': len(profits),
            'avg_return': round(statistics.mean(profits), 2),
            'win_rate': round(len(sr_win) / len(profits) * 100, 1) if profits else 0,
        }

    # 最大回撤
    max_drawdown = _calc_drawdown(multi_trades)

    report = {
        'strategy': strategy_name,
        'params': {k: str(v) for k, v in p.items()},
        'total_stocks': len(stocks),
        'total_trades': total_trades,
        'win_trades': win_count,
        'lose_trades': lose_count,
        'win_rate': round(win_rate, 2),
        'avg_win_pct': round(avg_win, 2),
        'avg_lose_pct': round(abs(avg_lose), 2),
        'profit_factor': round(profit_factor, 2),
        'total_return': round(total_return, 2),
        'avg_hold_days': round(avg_hold, 1),
        'max_drawdown': round(max_drawdown, 2),
        'hold_stats': hold_stats,
        'exit_stats': exit_stats,
        'score_stats': score_stats,
        'trade_count': len(multi_trades),
        'trades_sample': multi_trades[:50],  # 前50条明细
    }

    return report


def _simulate_ladder(ts_code, p):
    """对单只股票执行阶梯策略模拟"""
    with db_cursor(commit=False) as cur:
        # 获取完整K线
        cur.execute("""
            SELECT trade_date, open, high, low, close, change_pct, vol
            FROM daily_kline
            WHERE ts_code=%s
            ORDER BY trade_date ASC
        """, [ts_code])
        klines = cur.fetchall()

        if len(klines) < 30:
            return None

        # 获取季节状态（按交易日映射）  
        cur.execute("""
            SELECT trade_date, season FROM season_state
            ORDER BY trade_date ASC
        """)
        season_rows = cur.fetchall()
    season_map = {}
    for sr in season_rows:
        key = str(sr['trade_date'])
        if hasattr(sr['trade_date'], 'strftime'):
            key = sr['trade_date'].strftime('%Y-%m-%d')
        season_map[key] = sr['season'] if sr['season'] else ''

    trades = []
    holding = None  # {buy_date, buy_price, buy_score, peak_price}

    buy_score = int(p['buy_score'])
    stop_5d_score = int(p['stop_5d_score'])
    stop_drawdown = float(p['stop_drawdown'])
    check_10_score = int(p['check_10_score'])
    check_20_score = int(p['check_20_score'])
    check_30_score = int(p['check_30_score'])
    half_loss = float(p['half_loss'])
    half_score = int(p['half_score'])
    max_hold = int(p['max_hold'])
    trailing_stop = float(p['trailing_stop'])

    # 按交易日循环
    trade_dates_list = [str(k['trade_date']) if hasattr(k['trade_date'], 'strftime') else str(k['trade_date'])[:10] 
                        for k in klines]

    for idx in range(0, len(klines)):
        row = klines[idx]
        date_key = str(row['trade_date']) if hasattr(row['trade_date'], 'strftime') else str(row['trade_date'])[:10]
        season = season_map.get(date_key, '')

        # 计算评分
        score = calc_simple_score(row, season)
        close = float(row['close'])
        change_pct = float(row['change_pct']) if row['change_pct'] else 0

        # ---- 买入逻辑 ----
        if holding is None and score >= buy_score:
            holding = {
                'buy_date': date_key,
                'buy_price': close,
                'buy_score': score,
                'peak_price': close,
                'half_done': False,
            }
            continue

        # ---- 持仓中逻辑 ----
        if holding is not None:
            hold_days = (datetime.strptime(date_key, '%Y-%m-%d') - 
                        datetime.strptime(holding['buy_date'], '%Y-%m-%d')).days
            
            # 更新最高价
            if close > holding['peak_price']:
                holding['peak_price'] = close

            # 计算盈亏
            profit_pct = (close - holding['buy_price']) / holding['buy_price'] * 100

            # 计算从最高点的回撤
            drawdown = (holding['peak_price'] - close) / holding['peak_price'] * 100 if holding['peak_price'] > 0 else 0

            # 判定是否卖出
            sell = False
            sell_reason = ''

            # 5日止损：评分低于止损线
            if hold_days <= 5 and score < stop_5d_score:
                sell = True
                sell_reason = f'5日止损(评分{score}<{stop_5d_score})'

            # 回撤止损
            if not sell and drawdown > stop_drawdown:
                sell = True
                sell_reason = f'回撤止损({drawdown:.1f}>{stop_drawdown}%)'

            # 移动止盈
            if not sell and drawdown > trailing_stop and profit_pct > 10:
                sell = True
                sell_reason = f'移动止盈(回撤{drawdown:.1f}>{trailing_stop}%)'

            # 减半仓逻辑（亏损>half_loss且评分<half_score）
            if not sell and not holding['half_done'] and profit_pct < -half_loss and score < half_score:
                holding['half_done'] = True
                # 这里模拟减半仓：记录一次半仓交易
                half_profit = profit_pct / 2  # 假设减半仓后剩余一半
                trades.append({
                    'ts_code': ts_code,
                    'buy_date': holding['buy_date'],
                    'sell_date': date_key,
                    'hold_days': hold_days,
                    'buy_price': round(holding['buy_price'], 2),
                    'sell_price': round(close, 2),
                    'profit_pct': round(profit_pct, 2),
                    'half_remaining': True,
                    'exit_reason': f'减半仓(亏损{abs(profit_pct):.1f}评分{score}<{half_score})',
                    'buy_score': holding['buy_score'],
                })
                holding['buy_price'] = close  # 剩余部分按现价重新计算成本
                holding['buy_date'] = date_key
                continue

            # 检查点卖出
            if not sell:
                if hold_days >= 30:
                    if score < check_30_score:
                        sell = True
                        sell_reason = f'30日检查(评分{score}<{check_30_score})'
                elif hold_days >= 20:
                    if score < check_20_score:
                        sell = True
                        sell_reason = f'20日检查(评分{score}<{check_20_score})'
                elif hold_days >= 10:
                    if score < check_10_score:
                        sell = True
                        sell_reason = f'10日检查(评分{score}<{check_10_score})'

            # 最长持有到期
            if not sell and hold_days >= max_hold:
                sell = True
                sell_reason = f'最长持有{max_hold}日到期'

            if sell:
                trades.append({
                    'ts_code': ts_code,
                    'buy_date': holding['buy_date'],
                    'sell_date': date_key,
                    'hold_days': hold_days,
                    'buy_price': round(holding['buy_price'], 2),
                    'sell_price': round(close, 2),
                    'profit_pct': round(profit_pct, 2),
                    'exit_reason': sell_reason,
                    'buy_score': holding['buy_score'],
                })
                holding = None

    # 最后强制平仓
    if holding is not None and klines:
        last = klines[-1]
        last_date = str(last['trade_date']) if hasattr(last['trade_date'], 'strftime') else str(last['trade_date'])[:10]
        last_close = float(last['close'])
        hold_days = (datetime.strptime(last_date, '%Y-%m-%d') - 
                    datetime.strptime(holding['buy_date'], '%Y-%m-%d')).days
        profit_pct = (last_close - holding['buy_price']) / holding['buy_price'] * 100
        trades.append({
            'ts_code': ts_code,
            'buy_date': holding['buy_date'],
            'sell_date': last_date,
            'hold_days': hold_days,
            'buy_price': round(holding['buy_price'], 2),
            'sell_price': round(last_close, 2),
            'profit_pct': round(profit_pct, 2),
            'exit_reason': '强制平仓',
            'buy_score': holding['buy_score'],
        })

    if not trades:
        return None

    # 统计
    wins = [t for t in trades if t['profit_pct'] > 0]
    losses = [t for t in trades if t['profit_pct'] <= 0]
    total_ret = sum(t['profit_pct'] for t in trades)

    return {
        'trades': trades,
        'total': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'total_return': total_ret,
    }


def _gen_strategy_name(p):
    """生成策略名"""
    return f"阶梯策略_B{p['buy_score']}_S{p['stop_5d_score']}_D{p['stop_drawdown']}_M{p['max_hold']}"


def _hold_segment(days):
    if days <= 5:
        return '1-5日'
    elif days <= 10:
        return '6-10日'
    elif days <= 20:
        return '11-20日'
    elif days <= 30:
        return '21-30日'
    elif days <= 60:
        return '31-60日'
    else:
        return '60日+'


def _score_range(score):
    if score >= 80:
        return '80-100'
    elif score >= 60:
        return '60-79'
    elif score >= 40:
        return '40-59'
    else:
        return '0-39'


def _calc_drawdown(trades):
    """计算最大回撤（按交易序列）"""
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


def list_pool():
    """获取回测池列表"""
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT bp.*, COALESCE(wp.is_active, 0) as in_watch
            FROM backtest_pool bp
            LEFT JOIN watch_pool wp ON bp.ts_code = wp.ts_code
            ORDER BY bp.industry, bp.name
        """)
        return serialize_rows(cur.fetchall())


def manage_pool(action, ts_code, name=None, industry=None):
    """管理回测池"""
    with db_cursor() as cur:
        if action == 'add':
            cur.execute("""
                INSERT INTO backtest_pool (ts_code, name, industry)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE is_active=1
            """, [ts_code, name or '', industry or ''])
            return {'ts_code': ts_code}
        elif action == 'remove':
            cur.execute("UPDATE backtest_pool SET is_active=0 WHERE ts_code=%s", [ts_code])
            return {'ts_code': ts_code}
        elif action == 'restore':
            cur.execute("UPDATE backtest_pool SET is_active=1 WHERE ts_code=%s", [ts_code])
            return {'ts_code': ts_code}
        elif action == 'batch_add' and ts_code:
            codes = ts_code.split(',')
            count = 0
            for c in codes:
                c = c.strip()
                if c:
                    cur.execute("""
                        INSERT IGNORE INTO backtest_pool (ts_code, name, industry)
                        SELECT %s, name, industry FROM stock_basic WHERE ts_code=%s
                    """, [c, c])
                    count += 1
            return {'added': count}
    return None
