#!/usr/bin/env python3
"""
V13.1 回测 — 含置信度动态校准 + 分季参数差异化
周期: 2026-01-01 ~ 2026-07-04
数据源: backtest_score_daily (子因子+行情) + season_state (置信度)
公式: composite = trend×0.40 + structure×0.10 + momentum×0.25 + mf×0.25
校准: 百分位映射 + confidence压缩
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

# ── V13.1 分季参数 (buy_line, max_hold, t1%, t2%, trail%, max_pos%, max_total%) ──
SEASON_PARAMS = {
    'summer':         (75, 30, 7.0, 5.0, 15.0, 20, 30),
    'spring':         (75, 30, 7.0, 5.0, 15.0, 20, 30),
    'weak_spring':    (75, 30, 7.0, 5.0, 15.0, 20, 30),
    'chaos':          (75, 30, 7.0, 5.0, 15.0, 20, 30),
    'chaos_spring':   (75, 30, 7.0, 5.0, 15.0, 20, 30),
    'chaos_autumn':   (75, 30, 7.0, 5.0, 15.0, 20, 15),
    'autumn':         (80, 30, 7.0, 5.0, 15.0, 20, 20),
    'weak_autumn':    (75, 30, 7.0, 5.0, 15.0, 20, 30),
    'winter':         (80, 30, 7.0, 5.0, 15.0, 20, 10),
}

SEASON_ALIAS = {
    'spring': 'weak_spring',
}

def confidence_scale(confidence):
    if confidence >= 0.7: return 1.0
    elif confidence >= 0.5: return 0.875
    elif confidence >= 0.3: return 0.625
    else: return 0.50

def calibrate_score(raw_score, all_raw_scores, scale):
    if not all_raw_scores:
        return max(0, min(100, raw_score))
    sorted_scores = sorted(all_raw_scores)
    n = len(sorted_scores)
    targets = {5: int(10*scale), 10: int(15*scale), 15: int(18*scale),
               20: int(20*scale), 25: int(22*scale), 30: int(24*scale),
               35: int(26*scale), 40: int(28*scale), 45: int(29*scale),
               50: int(30*scale), 55: int(32*scale),
               60: int(34*scale), 65: int(36*scale), 70: int(38*scale),
               75: int(40*scale), 80: int(44*scale),
               85: int(48*scale), 90: int(50*scale), 93: int(55*scale),
               95: int(60*scale), 97: int(68*scale), 99: int(75*scale),
               100: int(80*scale)}
    p100_target = targets[100]
    calib_map = {}
    for pct, t in targets.items():
        idx = min(int(n * pct / 100), n - 1)
        calib_map[sorted_scores[idx]] = t
    if sorted_scores:
        calib_map[sorted_scores[0]] = max(0, targets[5] - 5)
        calib_map[sorted_scores[-1]] = p100_target
    sorted_raws = sorted(calib_map.keys())
    if raw_score <= sorted_raws[0]: return float(calib_map[sorted_raws[0]])
    if raw_score >= sorted_raws[-1]: return float(calib_map[sorted_raws[-1]])
    for i in range(len(sorted_raws) - 1):
        lo, hi = sorted_raws[i], sorted_raws[i + 1]
        if lo <= raw_score <= hi:
            if hi == lo: return float(calib_map[lo])
            ratio = (raw_score - lo) / (hi - lo)
            return round(calib_map[lo] + ratio * (calib_map[hi] - calib_map[lo]), 1)
    return round(raw_score, 1)


def load_season_cache(conn, start_date, end_date):
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


def load_score_dates(conn, start_date, end_date):
    """列出有数据的交易日"""
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT trade_date FROM backtest_score_daily
        WHERE trade_date >= %s AND trade_date <= %s
          AND chanlun_trend IS NOT NULL
        ORDER BY trade_date
    """, (start_date, end_date))
    dates = [str(r['trade_date']) for r in c.fetchall()]
    c.close()
    return dates


def load_day_scores(conn, td):
    """加载当日所有股票的子因子+行情"""
    c = conn.cursor()
    c.execute("""
        SELECT ts_code, chanlun_trend, structure_score, momentum_score, mf_score, close_price
        FROM backtest_score_daily
        WHERE trade_date=%s AND chanlun_trend IS NOT NULL
    """, (td,))
    scores = {}
    for r in c.fetchall():
        try:
            scores[r['ts_code']] = {
                'trend': float(r['chanlun_trend'] or 50),
                'structure': float(r['structure_score'] or 50),
                'momentum': float(r['momentum_score'] or 50),
                'mf': float(r['mf_score'] or 50),
                'close': float(r['close_price'] or 0),
            }
        except Exception as e:
            pass
    c.close()
    return scores


def get_hold_prices(conn, ts_code, buy_date, max_hold):
    """获取持仓期间的价格序列（用于逐日止损检查）"""
    c = conn.cursor()
    end_dt = datetime.strptime(buy_date, '%Y-%m-%d') + timedelta(days=max_hold + 30)
    end_date = end_dt.strftime('%Y-%m-%d')
    c.execute("""
        SELECT trade_date, close_price FROM backtest_score_daily
        WHERE ts_code=%s AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (ts_code, buy_date, end_date))
    prices = [(str(r['trade_date']), float(r['close_price'])) for r in c.fetchall()]
    c.close()
    return prices


def simulate_trade_exit(ts_code, buy_date, buy_price, params, season):
    """
    模拟单笔交易退出
    返回退出信息或None（未触发）
    """
    _, max_hold, sl_pct, asl_pct, tr_pct, _, _ = params
    sl_t1 = sl_pct / 100.0
    sl_t2 = asl_pct / 100.0
    tr = tr_pct / 100.0

    price_series = get_hold_prices(conn, ts_code, buy_date, max_hold)
    if not price_series:
        return None

    peak = buy_price
    for hold_idx, (td, cp) in enumerate(price_series):
        if td == buy_date:
            continue
        hold_days = hold_idx
        profit_pct = (cp - buy_price) / buy_price

        if cp > peak:
            peak = cp

        # 移动止盈
        if tr > 0 and peak > buy_price:
            dd = (peak - cp) / peak
            if dd >= tr:
                pnl = (cp - buy_price) * 100 - buy_price * 100 * CHARGE_RATE - cp * 100 * CHARGE_RATE
                return {'exit_date': td, 'exit_price': cp, 'hold_days': hold_days,
                        'profit_pct': round(profit_pct * 100, 2), 'pnl': round(pnl, 2),
                        'reason': f'止盈(回撤{dd*100:.1f}%)'}
        # 止损T1
        if profit_pct <= -sl_t1:
            pnl = (cp - buy_price) * 100 - buy_price * 100 * CHARGE_RATE - cp * 100 * CHARGE_RATE
            return {'exit_date': td, 'exit_price': cp, 'hold_days': hold_days,
                    'profit_pct': round(profit_pct * 100, 2), 'pnl': round(pnl, 2),
                    'reason': f'止损T1({profit_pct*100:.1f}%)'}
        # 止损T2(第2天起)
        if hold_days >= 2 and profit_pct <= -sl_t2:
            pnl = (cp - buy_price) * 100 - buy_price * 100 * CHARGE_RATE - cp * 100 * CHARGE_RATE
            return {'exit_date': td, 'exit_price': cp, 'hold_days': hold_days,
                    'profit_pct': round(profit_pct * 100, 2), 'pnl': round(pnl, 2),
                    'reason': f'止损T2({profit_pct*100:.1f}%)'}
        # 到期
        if hold_days >= max_hold:
            pnl = (cp - buy_price) * 100 - buy_price * 100 * CHARGE_RATE - cp * 100 * CHARGE_RATE
            return {'exit_date': td, 'exit_price': cp, 'hold_days': hold_days,
                    'profit_pct': round(profit_pct * 100, 2), 'pnl': round(pnl, 2),
                    'reason': f'到期({hold_days}d)'}

    # 数据不够，用最后价格
    if price_series:
        last_td, last_cp = price_series[-1]
        if last_td != buy_date:
            profit_pct = (last_cp - buy_price) / buy_price
            hold_days = len(price_series) - 1
            pnl = (last_cp - buy_price) * 100 - buy_price * 100 * CHARGE_RATE - last_cp * 100 * CHARGE_RATE
            return {'exit_date': last_td, 'exit_price': last_cp, 'hold_days': hold_days,
                    'profit_pct': round(profit_pct * 100, 2), 'pnl': round(pnl, 2),
                    'reason': '数据结束'}
    return None


def backtest(start_date='2026-01-01', end_date='2026-07-04'):
    print(f"\n🚀 V13.1 回测: {start_date} ~ {end_date}")
    print(f"{'='*55}")

    print("🔄 加载季节/置信度...")
    season_cache = load_season_cache(conn, start_date, end_date)
    print(f"  ✓ {len(season_cache)}天")

    print("🔄 加载交易日列表...")
    trade_dates = load_score_dates(conn, start_date, end_date)
    print(f"  ✓ {len(trade_dates)}个交易日（有子因子数据）")

    cash = INIT_CAPITAL
    positions = []
    all_trades = []
    portfolio_values = []
    # 缓存当日评分（按日加载）
    day_score_cache = {}

    wins, losses = 0, 0
    t0 = time.time()
    missing_sub_factor = 0

    for idx, td in enumerate(trade_dates):
        if td not in season_cache:
            continue

        day_info = season_cache[td]
        cur_season = day_info['season']
        confidence = day_info['confidence']
        scale = confidence_scale(confidence)

        param_key = cur_season
        if param_key not in SEASON_PARAMS:
            param_key = SEASON_ALIAS.get(param_key, 'chaos')
        params = SEASON_PARAMS.get(param_key, SEASON_PARAMS['chaos'])
        buy_line, max_hold = params[0], params[1]
        max_pos_pct, max_total_pct = params[5], params[6]

        # ── 检查持仓退出 ──
        still_hold = []
        for p in positions:
            # 获取当日的收盘价（从持仓价格序列查）
            # 找最近的价格
            cur.execute("""
                SELECT close_price FROM backtest_score_daily
                WHERE ts_code=%s AND trade_date=%s LIMIT 1
            """, (p['ts_code'], td))
            r = cur.fetchone()
            if not r:
                still_hold.append(p)
                continue
            cur_close = float(r['close_price'])

            profit_pct = (cur_close - p['buy_price']) / p['buy_price']
            hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
            p['peak_price'] = max(p.get('peak_price', p['buy_price']), cur_close)

            exit_reason = None
            # 止损T1
            if profit_pct <= -params[2]/100.0:
                exit_reason = f'止损T1({profit_pct*100:.1f}%)'
            # 止损T2(>=2天)
            elif hold_days >= 2 and profit_pct <= -params[3]/100.0:
                exit_reason = f'止损T2({profit_pct*100:.1f}%)'
            # 移动止盈
            elif params[4] > 0 and p['peak_price'] > p['buy_price']:
                dd = (p['peak_price'] - cur_close) / p['peak_price']
                if dd >= params[4]/100.0:
                    exit_reason = f'止盈(回撤{dd*100:.1f}%)'
            # 到期
            elif hold_days >= max_hold:
                exit_reason = f'到期({hold_days}d)'

            if exit_reason:
                gross = cur_close * p['shares']
                pnl = gross - p['cost'] - gross * CHARGE_RATE
                cash += gross - gross * CHARGE_RATE
                all_trades.append({
                    **p, 'exit_date': td, 'exit_price': cur_close,
                    'hold_days': hold_days, 'profit_pct': round(profit_pct * 100, 2),
                    'pnl': round(pnl, 2), 'reason': exit_reason,
                    'confidence': confidence, 'scale': scale,
                })
                if profit_pct > 0: wins += 1
                else: losses += 1
            else:
                still_hold.append(p)
        positions = still_hold

        # ── 买入 ──
        cur_pos_value = sum(p['cost'] for p in positions)
        max_total_value = INIT_CAPITAL * max_total_pct / 100.0
        max_single_value = INIT_CAPITAL * max_pos_pct / 100.0

        if cur_pos_value < max_total_value:
            # 懒加载当日评分
            if td not in day_score_cache:
                day_score_cache[td] = load_day_scores(conn, td)

            day_scores = day_score_cache[td]
            if not day_scores:
                continue

            # 计算V13.1原始分
            raw_scores = {}
            for code, f in day_scores.items():
                if f.get('close', 0) <= 0:
                    continue
                raw = f['trend'] * 0.40 + f['structure'] * 0.10 + f['momentum'] * 0.25 + f['mf'] * 0.25
                raw_scores[code] = (raw, f['close'])

            if not raw_scores:
                continue

            # 置信度校准
            all_raw_vals = [v[0] for v in raw_scores.values()]
            candidates = []
            for code, (raw, cprice) in raw_scores.items():
                cal = calibrate_score(raw, all_raw_vals, scale)
                if cal >= buy_line:
                    candidates.append((code, raw, cal, cprice))

            candidates.sort(key=lambda x: x[2], reverse=True)

            for code, raw, cal, cprice in candidates[:BUY_PER_DAY]:
                if any(p['ts_code'] == code for p in positions):
                    continue

                cur_pos_value = sum(p['cost'] for p in positions)
                if cur_pos_value >= max_total_value:
                    break

                available_cash = cash
                available_pos = max_total_value - cur_pos_value
                buy_amount = min(max_single_value, available_cash, available_pos)
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
                    'peak_price': cprice, 'params': params,
                    'season': cur_season, 'season_key': param_key,
                    'calibrated_score': cal, 'raw_score': raw,
                })

        # ── 每日净值 ──
        pos_mkt = sum(p['buy_price'] * p['shares'] for p in positions)
        portfolio_values.append((td, cash + pos_mkt))

        if (idx + 1) % 20 == 0:
            elap = time.time() - t0
            print(f"  📅 {td} ({idx+1}/{len(trade_dates)}) | 持仓{len(positions)}只 | 现金¥{cash/10000:.1f}万 | {len(all_trades)}笔 | {elap:.0f}s")

    # ── 结算 ──
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
    print(f"📊 V13.1 回测结果 ({start_date} ~ {end_date})")
    print(f"{'='*55}")
    print(f"初始资金: ¥{INIT_CAPITAL/10000:.0f}万")
    print(f"最终资金: ¥{final_value/10000:.0f}万")
    print(f"总收益率: {total_return:.2f}%")
    print(f"最大回撤: {max_dd:.2f}%")
    if max_dd > 0 and total_return != 0:
        print(f"卡玛比率: {total_return/max_dd:.2f}")
    print(f"总交易: {len(all_trades)}笔")
    if len(all_trades) > 0:
        print(f"胜率: {wins/(wins+losses)*100:.1f}% ({wins}胜/{losses}负)")
        print(f"总盈亏: ¥{total_pnl:.0f}")
        print(f"平均盈亏: ¥{total_pnl/len(all_trades):.0f}")
        avg_win = sum(t['pnl'] for t in profit_trades) / wins if wins > 0 else 0
        avg_loss = sum(t['pnl'] for t in loss_trades) / losses if losses > 0 else 0
        if avg_loss != 0:
            print(f"盈亏比: {abs(avg_win/avg_loss):.2f}")
        print(f"平均持仓: {sum(t['hold_days'] for t in all_trades)/len(all_trades):.1f}天")

    print(f"\n📂 按季节分析:")
    by_season = defaultdict(list)
    for t in all_trades:
        by_season[t['season']].append(t)
    # 按SEASON_PARAMS的顺序
    for s in ['summer', 'spring', 'chaos_spring', 'chaos', 'chaos_autumn', 'autumn', 'winter']:
        ts = by_season.get(s, [])
        if not ts: continue
        sw = len([t for t in ts if t['profit_pct'] > 0])
        sl = len([t for t in ts if t['profit_pct'] <= 0])
        total = len(ts)
        avg_ret = sum(t['profit_pct'] for t in ts) / total
        print(f"  {s}: {total}笔 | {sw/total*100:.1f}% | 均{avg_ret:+.2f}%")

    print(f"\n📂 持有期分布:")
    buckets = [(0,5),(5,10),(10,15),(15,20),(20,30),(30,60)]
    for lo, hi in buckets:
        ts = [t for t in all_trades if lo <= t['hold_days'] < hi]
        if ts:
            print(f"  {lo}-{hi}d: {len(ts)}笔 | {sum(1 for t in ts if t['profit_pct']>0)/len(ts)*100:.0f}% | 均{sum(t['profit_pct'] for t in ts)/len(ts):+.2f}%")

    print(f"\n🏆 TOP5盈利:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'], reverse=True)[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']}")

    print(f"\n💀 TOP5亏损:")
    for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
        print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']}")

    conn.close()
    t = time.time() - t0
    print(f"\n⏱ 耗时: {t:.0f}s")

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
