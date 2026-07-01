"""
strategy.py - 阶梯策略引擎
检查各检查点状态，生成操作建议
"""
import logging
from datetime import date, datetime
from db_config import db_cursor

logger = logging.getLogger('strategy_engine')


def check_all_holdings():
    """检查所有持仓的阶梯状态"""
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT id, ts_code, name, shares, cost_price, current_price,
                   buy_date, status
            FROM portfolio_holdings
            WHERE status IN ('HOLDING', 'hold', 'locked')
        """)
        holdings = cur.fetchall()

    results = []
    for h in holdings:
        result = check_single(h)
        if result:
            results.append(result)

    return results


def check_single(holding):
    """检查单只持仓的阶梯状态"""
    ts_code = holding['ts_code']
    buy_date = holding['buy_date']
    cost_price = float(holding['cost_price'])

    # 获取最新K线
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT trade_date, close, change_pct, high, low
            FROM daily_kline
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, [ts_code])
        latest = cur.fetchone()

    if not latest:
        return None

    current_price = float(latest['close'])
    change_pct = float(latest['change_pct'])
    trade_date = latest['trade_date']

    # 计算持有天数
    if buy_date:
        hold_days = (trade_date - buy_date).days
    else:
        hold_days = 0

    # 盈亏
    shares = float(holding['shares'])
    if cost_price > 0:
        profit_pct = (current_price - cost_price) / cost_price * 100
        profit_amount = (current_price - cost_price) * shares
    else:
        # 负数成本：盈亏率=盈亏金额/成本绝对值
        profit_amount = (current_price - cost_price) * shares
        cost_value = abs(cost_price * shares)
        if cost_value > 0:
            profit_pct = profit_amount / cost_value * 100
        else:
            profit_pct = 0

    # 检查点判定
    check_point = _determine_checkpoint(hold_days)

    # 检查止损/止盈
    action = _determine_action(hold_days, profit_pct, cost_price, current_price)

    # 获取最新评分
    latest_score = 0
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT composite_score, signal_type
            FROM strategy_signal
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, [ts_code])
        score_row = cur.fetchone()
        if score_row:
            latest_score = float(score_row['composite_score'])

    return {
        'ts_code': ts_code,
        'name': holding['name'],
        'hold_days': hold_days,
        'check_point': check_point,
        'profit_pct': round(profit_pct, 2),
        'current_price': current_price,
        'cost_price': cost_price,
        'shares': holding['shares'],
        'action': action['action'],
        'action_label': action['label'],
        'reason': action['reason'],
        'latest_score': latest_score,
        'trade_date': str(trade_date),
    }


def _determine_checkpoint(hold_days):
    """判定当前在第几个检查点"""
    from db_config import db_cursor as _dc

    try:
        with _dc(commit=False) as cur:
            cur.execute(
                "SELECT config_key, config_value FROM strategy_config "
                "WHERE config_key IN ('p1_check_day','p2_check_day','p3_check_day','p4_close_day')"
            )
            rows = cur.fetchall()
            config = {r['config_key']: int(float(r['config_value'])) for r in rows}
    except:
        config = {'p1_check_day': 5, 'p2_check_day': 15, 'p3_check_day': 25, 'p4_close_day': 30}

    if hold_days >= config.get('p4_close_day', 30):
        return f'P4-强制平仓({config["p4_close_day"]}日)'
    elif hold_days >= config.get('p3_check_day', 25):
        return f'P3-检查点({config["p3_check_day"]}日)'
    elif hold_days >= config.get('p2_check_day', 15):
        return f'P2-检查点({config["p2_check_day"]}日)'
    elif hold_days >= config.get('p1_check_day', 5):
        return f'P1-检查点({config["p1_check_day"]}日)'
    else:
        return f'持仓观察({hold_days}日)'


def _determine_action(hold_days, profit_pct, cost_price, current_price):
    """生成检查点操作建议"""
    from db_config import db_cursor as _dc

    try:
        with _dc(commit=False) as cur:
            cur.execute(
                "SELECT config_key, config_value FROM strategy_config "
                "WHERE config_key IN ('trailing_stop_pct','stop_loss_pct','p4_close_day','max_hold_days')"
            )
            rows = cur.fetchall()
            config = {r['config_key']: float(r['config_value']) for r in rows}
    except:
        config = {'trailing_stop_pct': 15.0, 'stop_loss_pct': -10.0,
                  'p4_close_day': 30, 'max_hold_days': 30}

    trailing_stop = config.get('trailing_stop_pct', 15)
    stop_loss = config.get('stop_loss_pct', -10)
    max_hold = int(config.get('max_hold_days', 30))
    p4_close = int(config.get('p4_close_day', 30))

    # 止损
    if profit_pct <= stop_loss:
        return {'action': 'SELL', 'label': '止损卖出',
                'reason': f'触发止损线({stop_loss}%)，亏损{round(abs(profit_pct),1)}%'}

    # 强制平仓
    if hold_days >= max_hold:
        return {'action': 'CLOSE', 'label': '强制平仓',
                'reason': f'持有超过{max_hold}日，强制平仓'}

    # P4到期
    if hold_days >= p4_close:
        return {'action': 'CLOSE', 'label': '到期平仓',
                'reason': f'到达P4检查点({p4_close}日)，建议止盈'}

    # 移动止盈（P3及以上）
    if hold_days >= int(config.get('p3_check_day', 25)) and profit_pct > 10:
        return {'action': 'HOLD_PARTIAL', 'label': '部分止盈',
                'reason': f'浮盈{round(profit_pct,1)}%，考虑减半仓锁定利润'}

    # P2检查
    if hold_days >= int(config.get('p2_check_day', 15)):
        if profit_pct > 5:
            return {'action': 'HOLD', 'label': '继续持有',
                    'reason': f'P2检查点，浮盈{round(profit_pct,1)}%，趋势良好'}
        elif profit_pct > -3:
            return {'action': 'WATCH', 'label': '观察等待',
                    'reason': f'P2检查点，盈亏{round(profit_pct,1)}%，建议继续观察'}
        else:
            return {'action': 'WATCH_SELL', 'label': '考虑减仓',
                    'reason': f'P2检查点，亏损{round(profit_pct,1)}%，考虑止损'}

    # P1检查
    if hold_days >= int(config.get('p1_check_day', 5)):
        if profit_pct > 5:
            return {'action': 'HOLD', 'label': '继续持有',
                    'reason': f'P1检查点，浮盈{round(profit_pct,1)}%，持有观望'}
        elif profit_pct > -5:
            return {'action': 'HOLD', 'label': '继续持有',
                    'reason': f'P1检查点，盈亏{round(profit_pct,1)}%，在预期范围内'}
        else:
            return {'action': 'WATCH', 'label': '密切观察',
                    'reason': f'P1检查点，亏损{round(profit_pct,1)}%，接近止损线'}

    # 正常持有
    if profit_pct > 0:
        return {'action': 'HOLD', 'label': '继续持有',
                'reason': f'持有{hold_days}日，浮盈{round(profit_pct,1)}%'}
    else:
        return {'action': 'HOLD', 'label': '继续持有',
                'reason': f'持有{hold_days}日，浮亏{round(abs(profit_pct),1)}%，等待反弹'}
