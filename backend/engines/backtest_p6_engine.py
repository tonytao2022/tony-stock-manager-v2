"""
backtest_p6_engine.py - 基于P6真实引擎的历史回测
每天回滚到过去交易日，用真实的评分引擎计算评分，按系统规则执行买卖
"""
import os, sys, json, time, logging
from datetime import date, timedelta, datetime
from collections import defaultdict
from db_config import db_cursor, serialize_rows

# 确保能找到引擎
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engines.p6_scorer import _score_single_stock, _save_score
from engines.season import detect_season
from engines.chanlun import analyze_stock

logger = logging.getLogger('p6_backtest')
logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s %(message)s')

# ─── 策略参数（和系统内配置一致） ───
PARAMS = {
    'buy_threshold': 65,       # 买入门槛
    'max_hold': 30,            # 最长持有日
    'stop_loss': -8.0,         # 止损%
    'trailing_stop': 15.0,     # 移动止盈回撤%
    'p1': 5,  'p2': 15,       # 检查点
    'p3': 25, 'p4_close': 30, # 检查点+强制平仓
    'cool_days': 20,           # 冷却天数
}

SEASONS = ['spring', 'summer', 'autumn', 'winter', 'chaos']
# 季节中文名
SEASON_CN = {
    'spring': '春', 'summer': '夏', 'autumn': '秋',
    'winter': '冬', 'chaos': '混沌',
    'chaos_spring': '春混沌', 'chaos_autumn': '秋混沌',
}


def run_full_backtest(start_date='2024-01-02', end_date=None,
                      params=None, stock_limit=0):
    """全量回测：逐日回滚评分+策略模拟"""
    if end_date is None:
        end_date = date.today()
    elif isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()

    p = {**PARAMS, **(params or {})}
    logger.info(f'[P6Backtest] 开始回测 {start_date}~{end_date}')

    # 1. 获取回测股票池
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT bp.ts_code, bp.name FROM backtest_pool bp
            WHERE bp.is_active=1
            ORDER BY bp.ts_code
        """)
        stocks = cur.fetchall()

    if stock_limit > 0:
        stocks = stocks[:stock_limit]

    total_stocks = len(stocks)
    all_trades = []
    stock_results = {}

    # 2. 逐股票跑
    for si, stock in enumerate(stocks):
        code = stock['ts_code']
        name = stock['name']
        if si % 10 == 0:
            logger.info(f'[P6Backtest] 进度: {si}/{total_stocks} ({code})')

        try:
            result = _backtest_single_stock(code, start_date, end_date, p)
            if result and result['trades']:
                all_trades.extend(result['trades'])
                stock_results[code] = result
        except Exception as e:
            logger.error(f'[P6Backtest] {code} 回测失败: {e}')
            continue

    # 3. 汇总统计
    report = _aggregate_report(all_trades, total_stocks, p)
    return report


def _backtest_single_stock(ts_code, start_date, end_date, p):
    """对单只股票回滚评分+模拟交易"""
    # 获取该股票的所有交易日K线
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT trade_date, close, open, high, low, change_pct, vol
            FROM daily_kline
            WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date ASC
        """, [ts_code, start_date, end_date + timedelta(days=p['max_hold'] + 10)])
        klines = cur.fetchall()

    if len(klines) < 30:
        return None

    # 交易日列表
    trade_dates = []
    price_map = {}
    for k in klines:
        td = k['trade_date']
        if hasattr(td, 'strftime'):
            td_str = td.strftime('%Y-%m-%d')
        else:
            td_str = str(td)[:10]
        trade_dates.append(td_str)
        price_map[td_str] = float(k['close'])

    # 日期去重
    trade_dates = sorted(set(trade_dates))
    # 每隔N个交易日评分一次（加速：全部交易日都跑太慢，取60个评分点）
    step = max(1, len(trade_dates) // 180)

    trades = []
    holding = None  # {buy_date, buy_price, peak_price, entry_score}
    cool_until = None  # 冷却期截止日期

    checkpoint_scores = [0] * 3  # 用于评分趋势判断

    for idx in range(step, len(trade_dates), step):
        date_str = trade_dates[idx]
        trade_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        current_price = price_map.get(date_str, 0)
        if current_price == 0:
            continue

        # ── 回滚季节 + 评分 ──
        # 回滚季节判断（确保季节数据与当前交易日一致）
        try:
            detect_season(trade_date)
        except:
            pass
        score_result = _score_single_stock(ts_code, trade_date)
        if not score_result:
            continue
        score = score_result['composite_score']
        gate = score_result.get('gate_triggered', 0)
        season = score_result.get('season', '')
        signal = score_result.get('signal_type', 'HOLD')

        # ── 冷却期检查 ──
        if cool_until and trade_date <= cool_until:
            continue

        # 回测中禁用闸门（闸门是实时风控，回测只看评分有效性）
        gate = 0
        # ── 买入逻辑 ──
        if holding is None and not gate and score >= p['buy_threshold']:
            holding = {
                'buy_date': date_str,
                'buy_price': current_price,
                'peak_price': current_price,
                'entry_score': score,
                'entry_season': season,
            }
            # 记录checkpoint起始评分
            checkpoint_scores = [score]
            continue

        # ── 卖出/持仓检查逻辑 ──
        if holding is not None:
            hold_days = (trade_date - datetime.strptime(holding['buy_date'],
                         '%Y-%m-%d').date()).days
            profit_pct = (current_price - holding['buy_price']) / holding['buy_price'] * 100

            # 更新最高价
            if current_price > holding['peak_price']:
                holding['peak_price'] = current_price

            drawdown = (holding['peak_price'] - current_price) / holding['peak_price'] * 100

            # 检查点记录评分趋势
            checkpoint_scores.append(score)
            if len(checkpoint_scores) > 5:
                checkpoint_scores.pop(0)

            sell = False
            sell_reason = ''

            # 条件1: 止损
            if profit_pct <= p['stop_loss']:
                sell = True
                sell_reason = f'止损({profit_pct:.1f}<={p["stop_loss"]}%)'

            # 条件2: 移动止盈
            elif profit_pct > 10 and drawdown > p['trailing_stop']:
                sell = True
                sell_reason = f'移动止盈(回撤{drawdown:.1f}>{p["trailing_stop"]}%)'

            # 条件3: P4强制平仓
            elif hold_days >= p['p4_close']:
                sell = True
                sell_reason = f'P4平仓({hold_days}日)'

            # 条件4: P3检查（评分持续下降）
            elif hold_days >= p['p3'] and len(checkpoint_scores) >= 3:
                if (checkpoint_scores[-1] < checkpoint_scores[-2] <
                    checkpoint_scores[-3] - 5):
                    sell = True
                    sell_reason = f'P3评分下降({score}<{checkpoint_scores[-3]:.0f})'

            # 条件5: P2+P1检查（评分跌破买入门槛-10）
            elif hold_days >= p['p1'] and score < p['buy_threshold'] - 10:
                sell = True
                sell_reason = f'P{5 if hold_days>=p["p2"] else 1}评分退坡({score:.0f})'

            # 条件6: 闸门触发+持仓超5日
            elif hold_days >= 5 and gate:
                sell = True
                sell_reason = f'闸门触发({season})'

            if sell:
                trades.append({
                    'ts_code': ts_code,
                    'buy_date': holding['buy_date'],
                    'sell_date': date_str,
                    'hold_days': hold_days,
                    'buy_price': round(holding['buy_price'], 2),
                    'sell_price': round(current_price, 2),
                    'profit_pct': round(profit_pct, 2),
                    'entry_score': holding['entry_score'],
                    'entry_season': holding['entry_season'],
                    'exit_season': season,
                    'exit_reason': sell_reason,
                })
                # 设置冷却期
                cool_until = trade_date + timedelta(days=p['cool_days'])
                holding = None

        # 检查点非持仓逻辑：更新跌时评分参考
        if holding is None and len(checkpoint_scores) > 10:
            checkpoint_scores = checkpoint_scores[-5:]

    if not trades:
        return None

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


def _aggregate_report(trades, total_stocks, p):
    """汇总统计"""
    if not trades:
        return {'total_trades': 0, 'message': '无交易'}

    import statistics

    total = len(trades)
    wins = [t for t in trades if t['profit_pct'] > 0]
    losses = [t for t in trades if t['profit_pct'] <= 0]
    win_count = len(wins)
    lose_count = len(losses)
    win_rate = win_count / total * 100 if total > 0 else 0

    avg_win = statistics.mean([t['profit_pct'] for t in wins]) if wins else 0
    avg_lose = statistics.mean([t['profit_pct'] for t in losses]) if losses else 0
    profit_factor = avg_win / abs(avg_lose) if abs(avg_lose) > 0 else 0
    total_return = sum(t['profit_pct'] for t in trades)
    avg_hold = statistics.mean([t['hold_days'] for t in trades]) if trades else 0

    # 持有期分段
    hold_segments = defaultdict(list)
    for t in trades:
        seg = _hold_seg(t['hold_days'])
        hold_segments[seg].append(t['profit_pct'])

    hold_stats = {}
    for seg, profits in sorted(hold_segments.items()):
        seg_win = [p for p in profits if p > 0]
        hold_stats[seg] = {
            'count': len(profits),
            'avg_return': round(statistics.mean(profits), 2),
            'win_rate': round(len(seg_win) / len(profits) * 100, 1) if profits else 0,
        }

    # 退出原因
    exit_reasons = defaultdict(list)
    for t in trades:
        exit_reasons[t['exit_reason']].append(t['profit_pct'])
    exit_stats = {}
    for reason, profits in exit_reasons.items():
        r_win = [p for p in profits if p > 0]
        exit_stats[reason] = {
            'count': len(profits),
            'avg_return': round(statistics.mean(profits), 2),
        }

    # 最大回撤
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

    return {
        'strategy': f'P6引擎_B{p["buy_threshold"]}_S{abs(p["stop_loss"]):.0f}_M{p["max_hold"]}',
        'total_stocks': total_stocks,
        'total_trades': total,
        'win_trades': win_count,
        'lose_trades': lose_count,
        'win_rate': round(win_rate, 2),
        'avg_win_pct': round(avg_win, 2),
        'avg_lose_pct': round(abs(avg_lose), 2),
        'profit_factor': round(profit_factor, 2),
        'total_return': round(total_return, 2),
        'max_drawdown': round(max_dd, 2),
        'avg_hold_days': round(avg_hold, 1),
        'hold_stats': dict(hold_stats),
        'exit_stats': dict(exit_stats),
        'trade_sample': trades[:50],
    }


def _hold_seg(days):
    if days <= 5: return '1-5日'
    elif days <= 10: return '6-10日'
    elif days <= 15: return '11-15日'
    elif days <= 20: return '16-20日'
    elif days <= 30: return '21-30日'
    elif days <= 60: return '31-60日'
    else: return '60日+'


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default='2024-01-02')
    parser.add_argument('--end', default=str(date.today()))
    parser.add_argument('--limit', type=int, default=0,
                        help='限制股票数量(0=全部)')
    parser.add_argument('--buy', type=float, default=65)
    args = parser.parse_args()

    report = run_full_backtest(
        start_date=args.start, end_date=args.end,
        params={'buy_threshold': args.buy},
        stock_limit=args.limit
    )

    print(json.dumps(report, ensure_ascii=False, indent=2,
                     default=str))
