#!/usr/bin/env python3
"""
V13.1 全量回测（基于strategy_signal全量历史评分）
周期: 2026-01-01 ~ 2026-07-04（先跑6个月验证）
     可选延长到2024-01-02
评分来源: strategy_signal.calibrated_score
季节来源: season_state.MARKET
参数：V13.1统一买入线75 + 分季仓位差异化 + 止损T1-7%/T2-5% + 移动止盈15%
"""
import sys, os, json, time, math, pymysql
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import _get_db_config

_cfg = _get_db_config()
conn = pymysql.connect(host=_cfg['host'], port=_cfg['port'], user=_cfg['user'],
                       password=_cfg['password'],
                       database=_cfg['database'], charset='utf8mb4',
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

INIT_CAPITAL = 1_000_000
BUY_PER_DAY = 3
CHARGE_RATE = 0.0005

# V13.1 分季参数
SEASON_PARAMS = {
    'summer':         {'buy':72, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'spring':         {'buy':72, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'weak_spring':    {'buy':72, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'chaos':          {'buy':72, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'chaos_spring':   {'buy':72, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'chaos_autumn':   {'buy':72, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':15},
    'autumn':         {'buy':76, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':20},
    'weak_autumn':    {'buy':72, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':30},
    'winter':         {'buy':76, 'hold':30, 't1':7.0, 't2':5.0, 'trail':15.0, 'max_pos':20, 'max_total':10},
}

SEASON_ALIAS = {'spring': 'weak_spring'}


def load_seasons(conn, start_date, end_date):
    """加载季节+置信度"""
    c = conn.cursor()
    c.execute("""
        SELECT trade_date, season, confidence
        FROM season_state WHERE index_code='MARKET'
          AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (start_date, end_date))
    cache = {}
    for r in c.fetchall():
        cache[str(r['trade_date'])] = {
            'season': r['season'],
            'confidence': float(r['confidence'] or 0.5),
        }
    c.close()
    return cache


def load_scores_by_day(conn, start_date, end_date):
    """加载每日所有股票的评分+收盘价"""
    c = conn.cursor()
    c.execute("""
        SELECT s.ts_code, s.trade_date, s.calibrated_score, s.trend_score, s.mf_score,
               dk.close
        FROM strategy_signal s
        LEFT JOIN daily_kline dk ON s.ts_code=dk.ts_code AND s.trade_date=dk.trade_date
        WHERE s.trade_date >= %s AND s.trade_date <= %s
          AND s.calibrated_score IS NOT NULL
        ORDER BY s.trade_date, s.ts_code
    """, (start_date, end_date))
    cache = defaultdict(dict)
    cnt = 0
    for r in c.fetchall():
        td = str(r['trade_date'])
        try:
            cache[td][r['ts_code']] = {
                'calibrated': float(r['calibrated_score'] or 0),
                'close': float(r['close'] or 0),
            }
            cnt += 1
        except:
            pass
    c.close()
    print(f"  ✓ 加载 {cnt} 条评分数据")
    return dict(cache)


def get_close_series(conn, ts_code, start_date, end_date):
    """获取持仓期间的价格序列"""
    c = conn.cursor()
    c.execute("""
        SELECT trade_date, close FROM daily_kline
        WHERE ts_code=%s AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (ts_code, start_date, end_date))
    prices = [(str(r['trade_date']), float(r['close'])) for r in c.fetchall()]
    c.close()
    return prices


def backtest(start_date='2026-01-01', end_date='2026-07-04'):
    print(f"\n🚀 V13.1 全量回测: {start_date} ~ {end_date}")
    print(f"{'='*55}")

    print("🔄 加载季节数据...")
    season_cache = load_seasons(conn, start_date, end_date)
    print(f"  ✓ {len(season_cache)}天")

    print("🔄 加载评分数据...")
    scores_by_day = load_scores_by_day(conn, start_date, end_date)
    trade_dates = sorted(scores_by_day.keys())
    print(f"  ✓ {len(trade_dates)}个交易日")

    cash = INIT_CAPITAL
    positions = []  # [{'ts_code','buy_date','buy_price','shares','cost','season','peak_price'}]
    all_trades = []
    portfolio_values = []

    t0 = time.time()
    total_days = len(trade_dates)

    for idx, td in enumerate(trade_dates):
        if td not in season_cache:
            continue

        day_info = season_cache[td]
        cur_season = day_info['season']

        param_key = cur_season
        if param_key not in SEASON_PARAMS:
            param_key = SEASON_ALIAS.get(param_key, 'chaos')
        sp = SEASON_PARAMS.get(param_key, SEASON_PARAMS['chaos'])
        buy_line = sp['buy']
        max_hold = sp['hold']
        max_pos = sp['max_pos']
        max_total = sp['max_total']
        t1 = sp['t1'] / 100.0
        t2 = sp['t2'] / 100.0
        tr = sp['trail'] / 100.0

        # ── 检查持仓退出 ──
        still_hold = []
        for p in positions:
            # 当日价
            cur.execute("SELECT close FROM daily_kline WHERE ts_code=%s AND trade_date=%s LIMIT 1",
                        (p['ts_code'], td))
            r = cur.fetchone()
            if not r:
                still_hold.append(p)
                continue
            cp = float(r['close'])

            hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cp - p['buy_price']) / p['buy_price']
            p['peak_price'] = max(p.get('peak_price', p['buy_price']), cp)

            exit_reason = None
            # 止损T1
            if profit_pct <= -t1:
                exit_reason = f'止损T1({profit_pct*100:.1f}%)'
            # 止损T2（>=2天）
            elif hold_days >= 2 and profit_pct <= -t2:
                exit_reason = f'止损T2({profit_pct*100:.1f}%)'
            # 移动止盈
            elif tr > 0 and p['peak_price'] > p['buy_price']:
                dd = (p['peak_price'] - cp) / p['peak_price']
                if dd >= tr:
                    exit_reason = f'止盈({dd*100:.1f}%)'
            # 到期
            elif hold_days >= max_hold:
                exit_reason = f'到期({hold_days}d)'

            if exit_reason:
                gross = cp * p['shares']
                pnl = gross - p['cost'] - gross * CHARGE_RATE
                cash += gross - gross * CHARGE_RATE
                all_trades.append({
                    **p, 'exit_date': td, 'exit_price': cp,
                    'hold_days': hold_days, 'profit_pct': round(profit_pct * 100, 2),
                    'pnl': round(pnl, 2), 'reason': exit_reason,
                })
            else:
                still_hold.append(p)
        positions = still_hold

        # ── 买入 ──
        cur_pos_value = sum(p['cost'] for p in positions)
        max_total_value = INIT_CAPITAL * max_total / 100.0

        if cur_pos_value < max_total_value and td in scores_by_day:
            day_scores = scores_by_day[td]

            # 按校准分排序
            candidates = [(code, info['calibrated'], info['close'])
                         for code, info in day_scores.items()
                         if info['calibrated'] >= buy_line and info['close'] > 0]
            candidates.sort(key=lambda x: x[1], reverse=True)

            for code, cal, cprice in candidates[:BUY_PER_DAY]:
                if any(p['ts_code'] == code for p in positions):
                    continue

                cur_pos_value = sum(p['cost'] for p in positions)
                if cur_pos_value >= max_total_value:
                    break

                available_cash = cash
                available_pos = max_total_value - cur_pos_value
                buy_amount = min(INIT_CAPITAL * max_pos / 100.0, available_cash, available_pos)
                if buy_amount < 10000:
                    continue

                shares = int(buy_amount / cprice / 100) * 100
                if shares < 100:
                    continue
                cost = shares * cprice * (1 + CHARGE_RATE)
                if cost > cash:
                    shares = int(cash * 0.98 / cprice / 100) * 100
                    if shares < 100:
                        continue
                    cost = shares * cprice * (1 + CHARGE_RATE)

                cash -= cost
                positions.append({
                    'ts_code': code, 'buy_date': td, 'entry_date': td,
                    'buy_price': cprice, 'entry_price': cprice,
                    'shares': shares, 'cost': cost, 'buy_charge': cprice * shares * CHARGE_RATE,
                    'peak_price': cprice,
                    'season': cur_season, 'season_key': param_key,
                    'calibrated_score': cal,
                })

        # 净值
        pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
        portfolio_values.append((td, cash + pos_mkt))

        if (idx + 1) % 40 == 0:
            elap = time.time() - t0
            print(f"  📅 {td} ({idx+1}/{total_days}) | 持仓{len(positions)}只 | ¥{cash/10000:.0f}万 | {len(all_trades)}笔 | {elap:.0f}s")

    # ── 汇总 ──
    final_pos_value = sum(p['buy_price'] * p['shares'] for p in positions)
    final_value = cash + final_pos_value
    total_return = (final_value - INIT_CAPITAL) / INIT_CAPITAL * 100

    peak = INIT_CAPITAL
    max_dd = 0
    for _, val in portfolio_values:
        if val > peak: peak = val
        dd = (peak - val) / peak * 100
        if dd > max_dd: max_dd = dd

    profit_trades = [t for t in all_trades if t['profit_pct'] > 0]
    loss_trades = [t for t in all_trades if t['profit_pct'] <= 0]
    total_pnl = sum(t['pnl'] for t in all_trades)
    wins = len(profit_trades)
    losses = len(loss_trades)

    print(f"\n{'='*55}")
    print(f"📊 V13.1 全量回测 ({start_date} ~ {end_date})")
    print(f"{'='*55}")
    print(f"初始: ¥{INIT_CAPITAL/10000:.0f}万")
    print(f"最终: ¥{final_value/10000:.0f}万")
    print(f"收益: {total_return:+.2f}%")
    print(f"回撤: {max_dd:.2f}%")
    if max_dd > 0:
        print(f"卡玛: {total_return/max_dd:.2f}")
    print(f"交易: {len(all_trades)}笔")
    if len(all_trades) > 0:
        print(f"胜率: {wins/(wins+losses)*100:.1f}% ({wins}胜/{losses}负)")
        print(f"总盈亏: ¥{total_pnl:.0f}")
        print(f"均值: ¥{total_pnl/len(all_trades):.0f}")
        avg_win = sum(t['pnl'] for t in profit_trades) / wins if wins else 0
        avg_loss = sum(t['pnl'] for t in loss_trades) / losses if losses else 0
        if avg_loss:
            print(f"盈亏比: {abs(avg_win/avg_loss):.2f}")
        print(f"均持有: {sum(t['hold_days'] for t in all_trades)/len(all_trades):.1f}d")

    # 按季节
    print(f"\n📂 季节分布:")
    by_season = defaultdict(list)
    for t in all_trades:
        by_season[t['season']].append(t)
    order = ['summer','spring','chaos_spring','chaos','chaos_autumn','autumn','winter']
    for s in order:
        ts = by_season.get(s, [])
        if not ts: continue
        sw = len([t for t in ts if t['profit_pct'] > 0])
        avg = sum(t['profit_pct'] for t in ts) / len(ts)
        print(f"  {s}: {len(ts)}笔 | {sw/len(ts)*100:.0f}% | 均{avg:+.2f}%")

    # 持有期
    print(f"\n📂 持有期:")
    for lo, hi in [(0,5),(5,10),(10,15),(15,20),(20,30),(30,60),(60,999)]:
        ts = [t for t in all_trades if lo <= t['hold_days'] < hi]
        if ts:
            print(f"  {lo}-{hi}d: {len(ts)}笔 | {sum(1 for t in ts if t['profit_pct']>0)/len(ts)*100:.0f}% | 均{sum(t['profit_pct'] for t in ts)/len(ts):+.2f}%")

    # Top
    print(f"\n🏆 TOP:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'], reverse=True)[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
    print(f"\n💀 BOTTOM:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")

    conn.close()
    elapsed = time.time() - t0
    print(f"\n⏱ {elapsed:.0f}s")

    return {
        'initial': INIT_CAPITAL, 'final': final_value,
        'return_pct': round(total_return, 2),
        'max_drawdown': round(max_dd, 2),
        'total_trades': len(all_trades),
        'win_rate': round(wins/(wins+losses)*100, 1) if (wins+losses) > 0 else 0,
        'total_pnl': round(total_pnl, 2),
        'profit_trades': wins, 'loss_trades': losses,
    }


if __name__ == '__main__':
    result = backtest('2026-01-01', '2026-07-04')
    print(json.dumps(result, indent=2))
