#!/usr/bin/env python3
"""
V13 全量回测 — V13四季参数矩阵
基于backtest_v122_full.py架构，参数替换为V13最终版
回测数据源：backtest_score_daily (已补到2026-06-29)
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

# ── 回测基础参数 ──
INIT_CAPITAL = 1_000_000
POS_LIMIT = 8
BUY_PER_DAY = 3
CHARGE_RATE = 0.0005

# ════════════════════════════════════════════
# 方案B：原现金15%逻辑 + 总仓位上限检查
# 每笔买入前检查总仓位占比是否超过季节对应的 max_total_pct
# ════════════════════════════════════════════

# ── V13 四季参数矩阵（最终版） ──
# 来源: v2-scores.html 页面V13参数表
V13_PARAMS = {
    'summer':         {'buy':65, 'hold':60, 't1':12, 't2':9,  'trailing':18, 'p4_min':55, 'p4_ext':15, 't2_on':True,  'max_pos_pct':50, 'cool_days':15},
    'spring':         {'buy':65, 'hold':20, 't1':8,  't2':6,  'trailing':12, 'p4_min':60, 'p4_ext':5,  't2_on':True,  'max_pos_pct':15, 'cool_days':15},
    'weak_spring':    {'buy':72, 'hold':20, 't1':8,  't2':6,  'trailing':12, 'p4_min':65, 'p4_ext':5,  't2_on':True,  'max_pos_pct':15, 'cool_days':15},
    'chaos':          {'buy':72, 'hold':25, 't1':10, 't2':8,  'trailing':12, 'p4_min':65, 'p4_ext':5,  't2_on':True,  'max_pos_pct':15, 'cool_days':15},
    'weak_autumn':    {'buy':80, 'hold':15, 't1':5,  't2':4,  'trailing':12, 'p4_min':999,'p4_ext':5,  't2_on':False, 'max_pos_pct':5,  'cool_days':15},
    'autumn':         {'buy':80, 'hold':15, 't1':5,  't2':4,  'trailing':12, 'p4_min':999,'p4_ext':0,  't2_on':False, 'max_pos_pct':8,  'cool_days':15},
    'winter':         {'buy':80, 'hold':10, 't1':5,  't2':4,  'trailing':8,  'p4_min':999,'p4_ext':0,  't2_on':False, 'max_pos_pct':5,  'cool_days':15},
    'chaos_spring':   {'buy':72, 'hold':20, 't1':8,  't2':6,  'trailing':12, 'p4_min':65, 'p4_ext':5,  't2_on':True,  'max_pos_pct':15, 'cool_days':15},
    'chaos_autumn':   {'buy':80, 'hold':15, 't1':5,  't2':4,  'trailing':12, 'p4_min':999,'p4_ext':0,  't2_on':False, 'max_pos_pct':5,  'cool_days':15},
}

# 方案B：季节→总仓位上限（max_total_pct %）
MAX_TOTAL_PCT_BY_SEASON = {
    'summer': 50,
    'spring': 30,
    'weak_spring': 30,
    'chaos': 15,
    'chaos_spring': 30,
    'chaos_autumn': 10,
    'weak_autumn': 10,
    'autumn': 15,
    'winter': 5,
}

# V13 季节→季节组别名映射（season_state中的season字段到参数key）
SEASON_ALIAS = {
    'summer': 'summer',
    'spring': 'spring',
    'weak_spring': 'weak_spring',
    'chaos': 'chaos',
    'chaos_spring': 'chaos_spring',
    'chaos_autumn': 'chaos_autumn',
    'weak_autumn': 'weak_autumn',
    'autumn': 'autumn',
    'winter': 'winter',
}


def get_season(td_str):
    """从season_state表获取指定日期的市场季节"""
    cur.execute("SELECT season FROM season_state WHERE index_code='MARKET' AND trade_date=%s", (td_str,))
    r = cur.fetchone()
    if r:
        return r['season']
    cur.execute("SELECT season FROM season_state WHERE index_code='MARKET' AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1", (td_str,))
    r = cur.fetchone()
    return r['season'] if r else 'chaos'


def get_score(ts_code, td_str):
    """从backtest_score_daily获取当日评分"""
    cur.execute("SELECT calibrated_score, composite_score, track, season, close_price FROM backtest_score_daily WHERE ts_code=%s AND trade_date=%s", (ts_code, td_str))
    r = cur.fetchone()
    if r:
        return {
            'calibrated': float(r['calibrated_score'] or 0),
            'composite': float(r['composite_score'] or 0),
            'track': r['track'] or '',
            'season': r['season'] or 'chaos',
            'close': float(r['close_price'] or 0),
        }
    return None


def get_close(ts_code, td_str):
    r = get_score(ts_code, td_str)
    return r['close'] if r else None


def get_hold_dates(ts_code, start_date, end_date):
    """持仓期间日线"""
    cur.execute("SELECT trade_date, close_price FROM backtest_score_daily WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (ts_code, start_date, end_date))
    return [(str(r['trade_date']), float(r['close_price'])) for r in cur.fetchall()]


def simulate_buy_sell(ts_code, buy_date, params, season):
    """模拟单笔交易"""
    max_hold = params['hold']
    t1 = params['t1']
    t2 = params['t2']
    t2_on = params['t2_on']
    trailing_stop = params['trailing']
    p4_min = params.get('p4_min', 0)
    p4_ext = params.get('p4_ext', 0)

    buy_close = get_close(ts_code, buy_date)
    if not buy_close or buy_close <= 0:
        return None

    buy_price = buy_close
    end_date_dt = datetime.strptime(buy_date, '%Y-%m-%d') + timedelta(days=max_hold * 2)
    end_date = end_date_dt.strftime('%Y-%m-%d')
    price_series = get_hold_dates(ts_code, buy_date, end_date)

    if not price_series:
        return None

    high_since_buy = buy_price
    hold_days = 0
    sell_price = buy_price
    sell_date = buy_date
    exit_reason = '到期平仓'

    for idx, (td, cp) in enumerate(price_series):
        if td <= buy_date:
            continue
        hold_days += 1
        profit_pct = (cp - buy_price) / buy_price * 100
        high_since_buy = max(high_since_buy, cp)
        drawdown = (high_since_buy - cp) / high_since_buy * 100

        # T1止损
        if t1 > 0 and profit_pct <= -t1:
            sell_price, sell_date, exit_reason = cp, td, 'T1止损'
            break

        # T2回撤（从高点）
        if t2_on and t2 > 0 and profit_pct > 3:
            if drawdown >= t2:
                sell_price, sell_date, exit_reason = cp, td, 'T2回撤'
                break

        # 移动止盈
        if trailing_stop > 0 and profit_pct > 5:
            if cp <= high_since_buy * (1 - trailing_stop / 100):
                sell_price, sell_date, exit_reason = cp, td, '移动止盈'
                break

        # P4延展
        p4_check = max_hold - p4_ext if p4_ext > 0 else max_hold
        if hold_days >= p4_check and profit_pct < p4_min:
            sell_price, sell_date, exit_reason = cp, td, 'P4未达标平仓'
            break

        # 到期
        if hold_days >= max_hold:
            sell_price, sell_date, exit_reason = cp, td, '到期平仓'
            break

    if sell_date == buy_date and price_series:
        sell_price = price_series[-1][1]
        sell_date = price_series[-1][0]
        exit_reason = '到期平仓'

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


def backtest(start_date='2024-09-02', end_date='2026-06-29'):
    t0 = time.time()

    # 1. 回测池（用watch_pool替代backtest_pool）
    cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
    pool = {r['ts_code']: r.get('name', '') for r in cur.fetchall()}
    codes = list(pool.keys())
    print(f"📊 V13 回测启动 | 池: {len(codes)}只 | 时间: {start_date}~{end_date}", flush=True)

    # 2. 交易日
    cur.execute("SELECT DISTINCT trade_date FROM backtest_score_daily WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (start_date, end_date))
    trade_days = [str(r['trade_date']) for r in cur.fetchall()]
    print(f"   交易日: {len(trade_days)}天", flush=True)
    if not trade_days:
        print("❌ 无交易日数据，终止", flush=True)
        return

    # 3. 逐日回测
    capital = INIT_CAPITAL
    positions = {}
    trades = []
    cool_until = {}  # {ts_code: date_str} 冷却期

    for day_idx, td_str in enumerate(trade_days):
        market_season = get_season(td_str)
        param_key = SEASON_ALIAS.get(market_season, 'chaos')
        params = V13_PARAMS.get(param_key, V13_PARAMS['chaos'])

        # 检查持仓
        to_sell = []
        for code, pos in list(positions.items()):
            result = simulate_buy_sell(code, pos['buy_date'], pos['params'], pos['season'])
            if result and result['sell_date'] == td_str:
                to_sell.append(result)

        for t in to_sell:
            code = t['ts_code']
            pos = positions.pop(code, None)
            if pos:
                profit = t['profit_pct']
                capital += pos['invested'] * (1 + profit / 100)
                t['capital'] = round(capital, 2)
                trades.append(t)
                # 每只平仓后进入冷却
                cool_days = pos['params'].get('cool_days', 15)
                cool_end = datetime.strptime(td_str, '%Y-%m-%d') + timedelta(days=cool_days)
                cool_until[code] = cool_end.strftime('%Y-%m-%d')

        # 检查冷却期
        cooled = []
        for code, until in list(cool_until.items()):
            if td_str >= until:
                cooled.append(code)
        for c in cooled:
            del cool_until[c]

        pos_count = len(positions)
        max_new = min(BUY_PER_DAY, POS_LIMIT - pos_count)
        if max_new <= 0:
            continue

        need_check = [c for c in codes if c not in positions and c not in cool_until]
        if not need_check:
            continue

        # 读取当日评分
        placeholders = ','.join(['%s'] * len(need_check))
        cur.execute(f"""
            SELECT ts_code, calibrated_score, track, season, composite_score
            FROM backtest_score_daily
            WHERE ts_code IN ({placeholders}) AND trade_date=%s
        """, (*need_check, td_str))
        day_scores = cur.fetchall()

        buy_line = params['buy']
        candidates = []
        for r in day_scores:
            sc = float(r['calibrated_score'] or 0)
            if sc >= buy_line:
                candidates.append({
                    'ts_code': r['ts_code'],
                    'score': sc,
                    'track': r['track'],
                    'season': r['season'],
                })

        candidates.sort(key=lambda x: -x['score'])

        for cand in candidates[:max_new]:
            code = cand['ts_code']
            if code in positions:
                continue

            buy_close = get_close(code, td_str)
            if not buy_close or buy_close <= 0:
                continue

            max_pos_pct = params['max_pos_pct']
            buy_amount = capital * max_pos_pct / 100
            buy_amount = min(buy_amount, capital * 0.5)
            buy_shares = int(buy_amount / buy_close / 100) * 100
            if buy_shares <= 0:
                continue

            actual_cost = buy_shares * buy_close * (1 + CHARGE_RATE)

            # 方案B：总仓位上限检查
            # 模拟购买后仓位 = 剩余现金扣除本次成本前的仓位市值总和
            # 注意：capital在购买后才会减去实际成本，所以当前现金=capital
            # 已持仓市值 = INIT_CAPITAL - capital （已买部分，不含本次新开仓位）
            total_used_pct = ((INIT_CAPITAL - capital) / INIT_CAPITAL) * 100
            season_for_cap = market_season
            cap_param_key = SEASON_ALIAS.get(season_for_cap, 'chaos')
            # 用season原值（市场季节名）查MAX_TOTAL_PCT_BY_SEASON
            max_total_pct = MAX_TOTAL_PCT_BY_SEASON.get(season_for_cap, 15)
            if total_used_pct >= max_total_pct:
                # 已达到总仓位上限，跳过本次买入
                continue
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
                'season': market_season,
                'params': params,
            }
            capital -= actual_cost

        # 进度
        if (day_idx + 1) % 50 == 0 or day_idx == 0:
            elapsed = time.time() - t0
            trades_done = sum(1 for t in trades if t.get('profit_pct', 0) > 0)
            total_done = len(trades)
            wr = trades_done / total_done * 100 if total_done > 0 else 0
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
            print(f"  [{day_idx+1}/{len(trade_days)}] {pct:.0f}% | 回报{total_ret:+.2f}% | "
                  f"交易{total_done}笔(胜{wr:.0f}%) | 持仓{len(positions)} | ETA~{eta_str}", flush=True)

    # 4. 清仓
    for code, pos in list(positions.items()):
        cp = get_close(code, trade_days[-1])
        if cp and pos['buy_price'] > 0:
            profit = (cp - pos['buy_price']) / pos['buy_price'] * 100
            capital += pos['invested'] * (1 + profit / 100)
            trades.append({
                'ts_code': code, 'buy_date': pos['buy_date'],
                'sell_date': trade_days[-1], 'hold_days': (datetime.strptime(trade_days[-1], '%Y-%m-%d') - datetime.strptime(pos['buy_date'], '%Y-%m-%d')).days,
                'buy_price': pos['buy_price'], 'sell_price': cp,
                'profit_pct': round(profit, 2), 'exit_reason': '持仓平仓',
                'season': pos['season'], 'capital': round(capital, 2),
            })

    # 5. 统计
    final_value = capital
    total_return = (final_value - INIT_CAPITAL) / INIT_CAPITAL * 100
    win_trades = [t for t in trades if t['profit_pct'] > 0]
    lose_trades = [t for t in trades if t['profit_pct'] <= 0]
    wr = len(win_trades) / len(trades) * 100 if trades else 0
    avg_win = sum(t['profit_pct'] for t in win_trades) / len(win_trades) if win_trades else 0
    avg_lose = abs(sum(t['profit_pct'] for t in lose_trades) / len(lose_trades)) if lose_trades else 1
    pf = avg_win / avg_lose if avg_lose > 0 else 0
    ah = sum(t['hold_days'] for t in trades) / len(trades) if trades else 0

    # 计算最大回撤
    peak = INIT_CAPITAL
    max_dd = 0
    cap_track = [INIT_CAPITAL]
    for t in sorted(trades, key=lambda x: x['sell_date'] + x['ts_code']):
        if t.get('capital'):
            peak = max(peak, t['capital'])
            dd = (peak - t['capital']) / peak * 100
            max_dd = max(max_dd, dd)
            cap_track.append(t['capital'])

    print("=" * 60, flush=True)
    print(f"📊 V13 全量回测结果", flush=True)
    print(f"   回测池: {len(codes)}只 | 时段: {start_date}~{end_date} ({len(trade_days)}天)", flush=True)
    print(f"   初始资金: ¥{INIT_CAPITAL:,.0f}", flush=True)
    print(f"   最终资金: ¥{final_value:,.2f}", flush=True)
    print(f"   总收益率: {total_return:+.2f}%", flush=True)
    print(f"   最大回撤: {max_dd:.2f}%", flush=True)
    print(f"   交易笔数: {len(trades)}", flush=True)
    print(f"   胜率: {wr:.2f}%", flush=True)
    print(f"   盈亏比: {pf:.2f}", flush=True)
    print(f"   平均持有: {ah:.1f}日", flush=True)
    print("=" * 60, flush=True)

    # 持有期分组
    by_hold = defaultdict(list)
    for t in trades:
        d = t['hold_days']
        if d <= 5: by_hold['1-5日'].append(t)
        elif d <= 10: by_hold['6-10日'].append(t)
        elif d <= 20: by_hold['11-20日'].append(t)
        elif d <= 30: by_hold['21-30日'].append(t)
        else: by_hold['31日+'].append(t)
    print("\n📈 持有期分组:", flush=True)
    for k, v in sorted(by_hold.items()):
        w = sum(1 for t in v if t['profit_pct'] > 0)
        wr2 = w / len(v) * 100 if v else 0
        a2 = sum(t['profit_pct'] for t in v) / len(v) if v else 0
        print(f"  {k}: {len(v)}笔 胜率{wr2:.1f}% 均收益{a2:+.2f}%", flush=True)

    # 季节分组
    by_season = defaultdict(list)
    for t in trades:
        by_season[t['season']].append(t)
    print("\n🍂 季节分组:", flush=True)
    for k in ['summer','spring','weak_spring','chaos','chaos_spring','chaos_autumn','weak_autumn','autumn','winter']:
        v = by_season.get(k, [])
        if v:
            w = sum(1 for t in v if t['profit_pct'] > 0)
            wr2 = w / len(v) * 100 if v else 0
            a2 = sum(t['profit_pct'] for t in v) / len(v) if v else 0
            print(f"  {k}: {len(v)}笔 胜率{wr2:.1f}% 均收益{a2:+.2f}%", flush=True)

    # 退出原因
    reasons = defaultdict(list)
    for t in trades:
        reasons[t['exit_reason']].append(t)
    print("\n🚪 退出原因分组:", flush=True)
    for rk in ['T1止损','T2回撤','移动止盈','P4未达标平仓','到期平仓','持仓平仓']:
        v = reasons.get(rk, [])
        if v:
            w2 = sum(1 for t in v if t['profit_pct'] > 0) / len(v) * 100
            a2 = sum(t['profit_pct'] for t in v) / len(v)
            print(f"  {rk}: {len(v)}笔 胜率{w2:.1f}% 均收益{a2:+.2f}%", flush=True)

    elapsed = time.time() - t0
    print(f"\n⏱️ 总耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)", flush=True)

    # 保存
    res = {
        'version': 'V13',
        'initial_capital': INIT_CAPITAL,
        'final_value': round(final_value, 2),
        'total_return_pct': round(total_return, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'trade_count': len(trades),
        'win_rate': round(wr, 2),
        'profit_factor': round(pf, 2),
        'avg_hold_days': round(ah, 1),
        'stock_count': len(codes),
        'trade_days': len(trade_days),
        'season_params': V13_PARAMS,
        'trades': trades,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    out = '/tmp/backtest_v13_B_result.json'
    with open(out, 'w') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out}", flush=True)
    # 方案B：输出到独立文本文件
    with open('/tmp/backtest_v13_B_result.txt', 'w') as rf:
        rf.write(f"V13 方案B 回测结果\n")
        rf.write(f"{'='*50}\n")
        rf.write(f"初始资金: ¥{INIT_CAPITAL:,.0f}\n")
        rf.write(f"最终资金: ¥{final_value:,.2f}\n")
        rf.write(f"总收益率: {total_return:+.2f}%\n")
        rf.write(f"最大回撤: {max_dd:.2f}%\n")
        rf.write(f"交易笔数: {len(trades)}\n")
        rf.write(f"胜率: {wr:.2f}%\n")
        rf.write(f"盈亏比: {pf:.2f}\n")
        rf.write(f"平均持有: {ah:.1f}日\n")
        rf.write(f"{'='*50}\n")
    print(f"结果文本已保存: /tmp/backtest_v13_B_result.txt", flush=True)
    return res


if __name__ == '__main__':
    backtest()
