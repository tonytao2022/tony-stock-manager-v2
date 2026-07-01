#!/usr/bin/env python3
"""
V12.2 全量回测 — 纯正V12.2基线版本
参数固化自 strategy_config_versions V8 (2026-06-16 14:37)

回测方式：每天对候选池逐只评分→买入，无简化
评分引擎：p6_dual_track_engine.score_stock (P6双轨引擎)
回测数据源：backtest_score_daily (P6引擎历史评分，已全量回填)
"""
import sys, os, json, time, math, pymysql, traceback
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p6_dual_track_engine import score_stock, MarketContext, _is_strong_stock
from season_engine import SeasonEngine
from db_config import _get_db_config

# ── 数据库连接 ──
_cfg = _get_db_config()
conn = pymysql.connect(host=_cfg['host'], port=_cfg['port'], user=_cfg['user'],
                       password=_cfg['password'],
                       database=_cfg['database'], charset='utf8mb4',
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

# ── 回测参数 ──
INIT_CAPITAL = 1_000_000
POS_LIMIT = 8
BUY_PER_DAY = 3
CHARGE_RATE = 0.0005  # 手续费万分之五

# ── V12.2 基线参数矩阵（2026-06-16 14:37 固化版本） ──
# 来源: strategy_config_versions V8，MAY方案最终版
V122_PARAMS = {
    'summer':         {'buy':68, 'hold':60, 't1':12, 't2':9,  'p4_min':55, 'p4_ext':15, 'trailing':18, 't2_on':True,  'max_pos_pct':40},
    'spring':         {'buy':65, 'hold':20, 't1':8,  't2':6,  'p4_min':60, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':15},
    'autumn':         {'buy':72, 'hold':25, 't1':8,  't2':6,  'p4_min':65, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':15},
    'winter':         {'buy':80, 'hold':10, 't1':5,  't2':4,  'p4_min':999,'p4_ext':0,  'trailing':8,  't2_on':False, 'max_pos_pct':5},
    'chaos':          {'buy':75, 'hold':25, 't1':10, 't2':8,  'p4_min':65, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':15},
    'chaos_spring':   {'buy':75, 'hold':20, 't1':8,  't2':6,  'p4_min':65, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':15},
    'chaos_autumn':   {'buy':75, 'hold':25, 't1':8,  't2':6,  'p4_min':65, 'p4_ext':5,  'trailing':12, 't2_on':True,  'max_pos_pct':10},
}

# 季节→评分轨道映射
SEASON_TRACK = {
    'summer': 'momentum', 'spring': 'momentum', 'chaos_spring': 'momentum',
    'autumn': 'reversion', 'winter': 'reversion', 'chaos_autumn': 'reversion',
    'chaos': 'reversion',
}


def get_season_and_strategy(td_str):
    """从season_state表获取指定日期的季节和评分策略"""
    cur.execute("""
        SELECT s.season, s.scoring_strategy
        FROM season_state s
        WHERE s.index_code='MARKET' AND s.trade_date=%s
    """, (td_str,))
    r = cur.fetchone()
    if r:
        return r['season'], r.get('scoring_strategy', SEASON_TRACK.get(r['season'], 'momentum'))

    # 退化：取最近一天的
    cur.execute("""
        SELECT season, scoring_strategy
        FROM season_state
        WHERE index_code='MARKET' AND trade_date<=%s
        ORDER BY trade_date DESC LIMIT 1
    """, (td_str,))
    r = cur.fetchone()
    if r:
        return r['season'], r.get('scoring_strategy', SEASON_TRACK.get(r['season'], 'momentum'))
    return 'chaos', 'momentum'


def get_score(ts_code, td_str):
    """从backtest_score_daily表获取当日评分（已有P6引擎历史评分）"""
    cur.execute("""
        SELECT composite_score, calibrated_score, track, season
        FROM backtest_score_daily
        WHERE ts_code=%s AND trade_date=%s
    """, (ts_code, td_str))
    r = cur.fetchone()
    if r:
        return {
            'score': float(r['calibrated_score'] or 0),
            'calibrated': float(r['calibrated_score'] or 0),
            'track': r['track'] or '',
            'season': r['season'] or 'chaos',
        }
    return None


def get_close(ts_code, td_str):
    """从backtest_score_daily获取收盘价"""
    cur.execute("""
        SELECT close_price FROM backtest_score_daily
        WHERE ts_code=%s AND trade_date=%s
    """, (ts_code, td_str))
    r = cur.fetchone()
    return float(r['close_price']) if r and r['close_price'] else None


def get_hold_dates(ts_code, start_date, end_date):
    """获取持仓期间的收盘价序列用于回撤计算"""
    cur.execute("""
        SELECT trade_date, close_price FROM backtest_score_daily
        WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date
    """, (ts_code, start_date, end_date))
    return [(str(r['trade_date']), float(r['close_price'])) for r in cur.fetchall()]


def simulate_buy_sell(ts_code, buy_date, buy_params, season):
    """
    模拟持仓管理
    buy_params: V12.2参数（hold/t1/t2/trailing/p4_min/p4_ext等）
    返回: {sell_date, hold_days, sell_price, profit_pct, exit_reason}
    """
    max_hold = buy_params['hold']
    t1 = buy_params['t1']
    t2 = buy_params['t2']
    t2_on = buy_params['t2_on']
    trailing_stop = buy_params['trailing']
    p4_min = buy_params.get('p4_min', 0)
    p4_ext = buy_params.get('p4_ext', 0)

    buy_close = get_close(ts_code, buy_date)
    if not buy_close or buy_close <= 0:
        return None

    buy_price = buy_close
    sell_price = None
    sell_date = None
    exit_reason = '到期平仓'

    # 获取持仓期间的日线
    start_date = buy_date
    end_date_dt = datetime.strptime(buy_date, '%Y-%m-%d') + timedelta(days=max_hold * 2)
    end_date = end_date_dt.strftime('%Y-%m-%d')
    price_series = get_hold_dates(ts_code, start_date, end_date)

    if not price_series:
        return None

    high_since_buy = buy_price
    hold_days = 0

    for idx, (td, cp) in enumerate(price_series):
        if td <= buy_date:
            continue
        hold_days += 1
        profit_pct = (cp - buy_price) / buy_price * 100
        high_since_buy = max(high_since_buy, cp)
        drawdown = (high_since_buy - cp) / high_since_buy * 100

        # T1止损
        if t1 > 0 and profit_pct <= -t1:
            sell_price = cp
            sell_date = td
            exit_reason = 'T1止损'
            break

        # T2回撤止盈（从高点回落）
        if t2_on and t2 > 0 and profit_pct > 3:
            if drawdown >= t2:
                sell_price = cp
                sell_date = td
                exit_reason = 'T2回撤'
                break

        # 移动止盈
        if trailing_stop > 0 and profit_pct > 5:
            if cp <= high_since_buy * (1 - trailing_stop / 100):
                sell_price = cp
                sell_date = td
                exit_reason = '移动止盈'
                break

        # P4延展：第4个检查点延后
        p4_check = max_hold - p4_ext if p4_ext > 0 else max_hold
        if hold_days >= p4_check and profit_pct < p4_min:
            sell_price = cp
            sell_date = td
            exit_reason = 'P4未达标平仓'
            break

        # 最大持有期到期
        if hold_days >= max_hold:
            sell_price = cp
            sell_date = td
            exit_reason = '到期平仓'
            break

    if sell_price is None:
        # 没有触发任何卖出条件，用最后价格
        if price_series:
            sell_price = price_series[-1][1]
            sell_date = price_series[-1][0]
        else:
            return None

    total_profit = (sell_price - buy_price) / buy_price * 100

    return {
        'ts_code': ts_code,
        'buy_date': buy_date,
        'sell_date': sell_date,
        'hold_days': hold_days,
        'buy_price': round(buy_price, 3),
        'sell_price': round(sell_price, 3),
        'profit_pct': round(total_profit, 2),
        'exit_reason': exit_reason,
        'season': season,
    }


def backtest():
    t0 = time.time()

    # 1. 获取回测池
    cur.execute("SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE'")
    pool = {r['ts_code']: r.get('name', '') for r in cur.fetchall()}
    codes = list(pool.keys())
    print("回测池: %d只" % len(codes), flush=True)

    # 2. 获取所有有评分的交易日
    cur.execute("""
        SELECT DISTINCT trade_date FROM backtest_score_daily
        WHERE trade_date >= '2023-01-01' AND trade_date <= '2026-06-18'
        ORDER BY trade_date
    """)
    trade_days = [str(r['trade_date']) for r in cur.fetchall()]
    print("交易日: %d天" % len(trade_days), flush=True)

    # 3. 逐日回测
    capital = INIT_CAPITAL
    positions = {}  # {ts_code: {buy_date, buy_price, params, season, hold_days}}
    trades = []
    day_start = time.time()

    for day_idx, td_str in enumerate(trade_days):
        # 获取当日季节
        season, _ = get_season_and_strategy(td_str)
        params = V122_PARAMS.get(season, V122_PARAMS['chaos'])

        # 检查持仓是否需卖出
        to_sell = []
        for code, pos in list(positions.items()):
            pos['hold_days'] = (pos.get('hold_days', 0) + 1)
            result = simulate_buy_sell(code, pos['buy_date'], params, pos['season'])
            if result and result['sell_date'] == td_str:
                to_sell.append(result)

        # 执行卖出
        for t in to_sell:
            code = t['ts_code']
            pos = positions.pop(code, None)
            if pos:
                profit = t['profit_pct']
                capital += pos['invested'] * (1 + profit / 100)
                t['capital'] = round(capital, 2)
                trades.append(t)

        # 计算可用资金和持仓数量
        pos_count = len(positions)
        max_new = min(BUY_PER_DAY, POS_LIMIT - pos_count)
        if max_new <= 0:
            continue

        # 获取当日评分排名（只取未持仓的）
        need_check = [c for c in codes if c not in positions]
        if not need_check:
            continue

        # 从backtest_score_daily读取当日评分排序
        placeholders = ','.join(['%s'] * len(need_check))
        cur.execute(f"""
            SELECT ts_code, calibrated_score, track, season
            FROM backtest_score_daily
            WHERE ts_code IN ({placeholders}) AND trade_date=%s
        """, (*need_check, td_str))
        day_scores = cur.fetchall()

        # 按评分排序
        candidates = []
        for r in day_scores:
            sc = float(r['calibrated_score'] or 0)
            buy_line = params['buy']
            if sc >= buy_line and r['season'] == season:
                candidates.append({
                    'ts_code': r['ts_code'],
                    'score': sc,
                    'track': r['track'],
                })

        candidates.sort(key=lambda x: -x['score'])

        # 买入（限当日最多BUY_PER_DAY只）
        for cand in candidates[:max_new]:
            code = cand['ts_code']
            if code in positions:
                continue

            buy_close = get_close(code, td_str)
            if not buy_close or buy_close <= 0:
                continue

            # 计算买入金额（按最大仓位比例）
            max_pos_pct = params['max_pos_pct']
            buy_amount = capital * max_pos_pct / 100
            buy_amount = min(buy_amount, capital * 0.5)  # 单笔不超过总资金50%
            buy_shares = int(buy_amount / buy_close / 100) * 100
            if buy_shares <= 0:
                continue

            actual_cost = buy_shares * buy_close * (1 + CHARGE_RATE)
            if actual_cost > capital:
                buy_shares = int(capital * 0.95 / buy_close / 100) * 100
                if buy_shares <= 0:
                    continue
                actual_cost = buy_shares * buy_close * (1 + CHARGE_RATE)

            positions[code] = {
                'buy_date': td_str,
                'buy_price': buy_close,
                'shares': buy_shares,
                'invested': actual_cost,
                'season': season,
                'params': params,
                'hold_days': 0,
            }
            capital -= actual_cost

        # 进度输出
        if (day_idx + 1) % 50 == 0 or day_idx == 0:
            elapsed = time.time() - t0
            trades_done = len([t for t in trades if t.get('profit_pct', 0) > 0])
            total_done = len(trades)
            wr = trades_done / total_done * 100 if total_done > 0 else 0
            total_pct = (capital - INIT_CAPITAL) / INIT_CAPITAL * 100
            # 加持仓浮动盈亏
            floating = 0
            for code, pos in positions.items():
                cp = get_close(code, td_str)
                if cp and pos['buy_price'] > 0:
                    floating += (cp - pos['buy_price']) / pos['buy_price'] * pos['invested']
            total_val = capital + floating
            total_ret = (total_val - INIT_CAPITAL) / INIT_CAPITAL * 100

            pct = (day_idx + 1) / len(trade_days) * 100
            eta = (time.time() - t0) / (day_idx + 1) * (len(trade_days) - day_idx - 1)
            eta_str = f"{eta/3600:.1f}h" if eta > 3600 else f"{eta/60:.0f}min"
            print(
                f"[{day_idx + 1}/{len(trade_days)}] {pct:.0f}% | 回报{total_ret:+.2f}% | "
                f"交易{total_done}笔(胜{wr:.0f}%) | 持仓{len(positions)} | "
                f"资金¥{capital:.0f} | ETA~{eta_str}",
                flush=True
            )

    # 4. 清仓剩余持仓
    for code, pos in list(positions.items()):
        cp = get_close(code, trade_days[-1])
        if cp and pos['buy_price'] > 0:
            profit = (cp - pos['buy_price']) / pos['buy_price'] * 100
            capital += pos['invested'] * (1 + profit / 100)
            trades.append({
                'ts_code': code,
                'buy_date': pos['buy_date'],
                'sell_date': trade_days[-1],
                'hold_days': pos['hold_days'],
                'buy_price': pos['buy_price'],
                'sell_price': cp,
                'profit_pct': round(profit, 2),
                'exit_reason': '持仓平仓',
                'season': pos['season'],
                'capital': round(capital, 2),
            })

    # 5. 统计结果
    final_value = capital
    total_return = (final_value - INIT_CAPITAL) / INIT_CAPITAL * 100

    win_trades = [t for t in trades if t['profit_pct'] > 0]
    lose_trades = [t for t in trades if t['profit_pct'] <= 0]
    wr = len(win_trades) / len(trades) * 100 if trades else 0

    avg_win = sum(t['profit_pct'] for t in win_trades) / len(win_trades) if win_trades else 0
    avg_lose = abs(sum(t['profit_pct'] for t in lose_trades) / len(lose_trades)) if lose_trades else 1
    pf = avg_win / avg_lose if avg_lose > 0 else 0
    ah = sum(t['hold_days'] for t in trades) / len(trades) if trades else 0

    print("=" * 60, flush=True)
    print(f"📊 V12.2 全量回测结果 ({len(codes)}只股票, {len(trade_days)}个交易日)", flush=True)
    print(f"   初始资金: ¥{INIT_CAPITAL:,.0f}", flush=True)
    print(f"   最终资金: ¥{final_value:,.2f}", flush=True)
    print(f"   总收益率: {total_return:+.2f}%", flush=True)
    print(f"   交易笔数: {len(trades)}", flush=True)
    print(f"   胜率: {wr:.2f}%", flush=True)
    print(f"   盈亏比: {pf:.2f}", flush=True)
    print(f"   平均持有: {ah:.1f}日", flush=True)
    print("=" * 60, flush=True)

    # 按持有期分组
    by_hold = defaultdict(list)
    for t in trades:
        if t['hold_days'] <= 5:
            by_hold['1-5日'].append(t)
        elif t['hold_days'] <= 10:
            by_hold['6-10日'].append(t)
        elif t['hold_days'] <= 20:
            by_hold['11-20日'].append(t)
        elif t['hold_days'] <= 30:
            by_hold['21-30日'].append(t)
        else:
            by_hold['31日+'].append(t)

    print("\n📈 持有期分组:", flush=True)
    for k, v in sorted(by_hold.items()):
        w = [t for t in v if t['profit_pct'] > 0]
        wr2 = len(w) / len(v) * 100 if v else 0
        avg2 = sum(t['profit_pct'] for t in v) / len(v) if v else 0
        print(f"  {k}: {len(v)}笔, 胜率{wr2:.1f}%, 均收益{avg2:+.2f}%", flush=True)

    # 按季节分组
    by_season = defaultdict(list)
    for t in trades:
        by_season[t['season']].append(t)

    print("\n🍂 季节分组:", flush=True)
    for k in ['summer', 'spring', 'autumn', 'winter', 'chaos', 'chaos_spring', 'chaos_autumn']:
        v = by_season.get(k, [])
        if v:
            w = [t for t in v if t['profit_pct'] > 0]
            wr2 = len(w) / len(v) * 100 if v else 0
            avg2 = sum(t['profit_pct'] for t in v) / len(v) if v else 0
            print(f"  {k}: {len(v)}笔, 胜率{wr2:.1f}%, 均收益{avg2:+.2f}%", flush=True)

    # 按退出原因分组
    reasons = defaultdict(list)
    for t in trades:
        reasons[t['exit_reason']].append(t)
    print("\n🚪 退出原因分组:", flush=True)
    for rk in ['T1止损', 'T2回撤', '移动止盈', 'P4未达标平仓', '到期平仓', '持仓平仓']:
        v = reasons.get(rk, [])
        if v:
            w2 = len([t for t in v if t['profit_pct'] > 0]) / len(v) * 100
            a2 = sum(t['profit_pct'] for t in v) / len(v)
            print(f"  {rk}: {len(v)}笔 胜率{w2:.1f}% 均收益{a2:+.2f}%", flush=True)

    elapsed = time.time() - t0
    print(f"\n⏱️ 总耗时: {elapsed:.0f}s ({elapsed / 60:.1f}min)", flush=True)

    # 保存结果
    res = {
        'version': 'V12.2',
        'initial_capital': INIT_CAPITAL,
        'final_value': round(final_value, 2),
        'total_return_pct': round(total_return, 2),
        'trade_count': len(trades),
        'win_rate': round(wr, 2),
        'profit_factor': round(pf, 2),
        'avg_hold_days': round(ah, 1),
        'stock_count': len(codes),
        'trade_days': len(trade_days),
        'season_params': V122_PARAMS,
        'trades': trades,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    out = '/tmp/backtest_v122_result.json'
    with open(out, 'w') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out}", flush=True)
    return res


if __name__ == '__main__':
    print("V12.2 全量回测启动 (620只, 2023~2026)...", flush=True)
    t0 = time.time()
    backtest()
    print(f"\n总耗时: {time.time() - t0:.0f}s", flush=True)
