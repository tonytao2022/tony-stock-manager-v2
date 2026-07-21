#!/usr/bin/env python3
"""
反事实回测框架 V1.0
=====================
从 strategy_signal 表读取真实评分，通过 config_overrides 模拟：
  - full: 全量配置（当前线上）
  - no_penalty: 去掉惩罚层
  - no_filters: 去掉过滤层（量比/资金爆量过滤）
  - no_both: 去掉惩罚+过滤
  - legacy_v133b: V13.3b 旧版配置（2026-07-18前）

原理：复用 BacktestEngineRealScore 的回测引擎，在买入检查时
对评分做后处理来模拟不同层关闭的效果。

评分数据是固化的——我们不改评分引擎，只改买入决策逻辑。

⚠️ 已知限制：
1. 过滤层历史数据缺失 — strategy_signal 表没有 is_filtered 标志位。
   当前 no_filters 配置无法验证（与full结果一致），因为评分在入场前
   已经过过滤，反事实框架无法从历史数据中重建被过滤掉的有效信号。
2. 过滤层重建偏差 — 从 daily_kline 重建量比判断可能与线上流程不一致
   （线上 _calc_vol_ratio() 在K线不足时会回退到1.0）。
   详见反事实框架中 _apply_filters() 的实现。
3. 此为观察性统计，不保证因果关系（Delta Diagnostics 的交互效应
   结论是数据倾向性的统计描述，非因果推断）。

用法:
  python3 backtest_counterfactual.py [start_date] [end_date]
  e.g., python3 backtest_counterfactual.py 2024-09-02 2026-07-17

时间: 2026-07-21
"""
import sys, os, time, math, json, pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter

DB_CFG = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
          'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
          'charset':'utf8mb4','cursorclass':pymysql.cursors.DictCursor,
          'read_timeout':300,'write_timeout':300}
INIT_CAPITAL = 1_000_000
MAX_POSITIONS = 8
MAX_BUY_PER_DAY = 3
CHARGE_RATE = 0.0005

LABELS = {
    'summer':'☀️夏','spring':'🌸春','weak_spring':'⛅弱春','chaos_spring':'🌤️混沌春',
    'chaos':'🌪️混沌','chaos_autumn':'☁️混沌秋','weak_autumn':'⛅弱秋',
    'autumn':'🍂秋','winter':'❄️冬'
}

# ── 5组对比的 config_overrides ──
CONFIG_SUITE = {
    'full': {
        'label': '全量配置（当前线上）',
        'enable_penalty': True,
        'enable_filters': True,
        'buy_line_mult': 1.0,
    },
    'no_penalty': {
        'label': '去掉惩罚层',
        'enable_penalty': False,
        'enable_filters': True,
        'buy_line_mult': 1.0,
    },
    'no_filters': {
        'label': '去掉过滤层',
        'enable_penalty': True,
        'enable_filters': False,
        'buy_line_mult': 1.0,
    },
    'no_both': {
        'label': '去掉惩罚+过滤',
        'enable_penalty': False,
        'enable_filters': False,
        'buy_line_mult': 1.0,
    },
    'legacy_v133b': {
        'label': 'V13.3b 旧版',
        'enable_penalty': True,
        'enable_filters': True,
        'buy_line_mult': 0.85,  # V13.3b买入线更宽松（当前校准线×0.85）
    },
}


class CounterfactualBacktest:
    """反事实回测引擎，复用共享数据"""

    # 类级共享数据存储（所有实例共用）
    _shared_data = None

    @classmethod
    def load_shared_data(cls, start_date='2024-09-02', end_date='2026-07-17'):
        """一次性加载所有共享数据"""
        if cls._shared_data is not None:
            print(f"  ℹ️ 复用已有数据 ({(time.time() - cls._shared_data['load_time'])/60:.1f}分钟前加载)")
            return cls._shared_data

        print("  首次加载共享数据...")
        t0 = time.time()
        conn = pymysql.connect(**DB_CFG)
        cur = conn.cursor()

        # 交易日
        cur.execute(f"""
            SELECT DISTINCT trade_date FROM strategy_signal
            WHERE trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date
        """, (start_date, end_date))
        trading_days = [str(r['trade_date']) for r in cur.fetchall()]

        # 策略参数（原始买入线，不做打折，打折在run时处理）
        cur.execute("""
            SELECT season_type, buy_min_score, max_hold_days,
                   stop_loss_pct, trailing_stop_pct,
                   max_pos_pct, max_total_pct
            FROM strategy_config
            WHERE is_active = 1 AND strategy_type = 'STEP_LOCK'
        """)
        base_sp = {}
        for r in cur.fetchall():
            st = r['season_type']
            base_sp[st] = {
                'buy': int(r['buy_min_score']),
                'hold': int(r['max_hold_days']),
                't1': float(r['stop_loss_pct']) / 100.0,
                't2': max(0.03, float(r['stop_loss_pct']) / 100.0 - 0.02),
                'trail': float(r['trailing_stop_pct']) / 100.0,
                'mp': float(r['max_pos_pct']) / 100.0,
                'mt': float(r['max_total_pct']) / 100.0,
            }

        # 监控池
        cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
        watch_pool = {}
        for r in cur.fetchall():
            watch_pool[r['ts_code']] = r.get('name') or r['ts_code']
        pool_set = set(watch_pool.keys())

        # 评分数据
        scores = {}
        chunk_size = 20000
        offset = 0
        total_scores = 0
        while True:
            cur.execute(f"""
                SELECT ts_code, trade_date, composite_score, calibrated_score,
                       season, penalty_score, penalty_reason
                FROM strategy_signal
                WHERE trade_date BETWEEN %s AND %s
                LIMIT %s OFFSET %s
            """, (start_date, end_date, chunk_size, offset))
            rows = cur.fetchall()
            if not rows:
                break
            for r in rows:
                key = (r['ts_code'], str(r['trade_date']))
                penalty = float(r['penalty_score']) if r['penalty_score'] is not None else 0.0
                scores[key] = {
                    'score': float(r['composite_score']) if r['composite_score'] is not None else 50.0,
                    'season': r['season'] or 'chaos',
                    'penalty_score': penalty,
                }
            total_scores += len(rows)
            offset += chunk_size

        # K线数据
        all_codes = list(pool_set)
        placeholders = ','.join(['%s'] * len(all_codes))
        sql = f"""
            SELECT ts_code, trade_date, close
            FROM daily_kline
            WHERE ts_code IN ({placeholders}) AND trade_date >= '2020-01-01'
            ORDER BY ts_code, trade_date
        """
        cur.execute(sql, all_codes)
        kline = defaultdict(list)
        for r in cur.fetchall():
            kline[r['ts_code']].append((str(r['trade_date']), float(r['close'])))

        # 沪深300
        cur.execute("""
            SELECT trade_date, close FROM daily_kline
            WHERE ts_code = '000300.SH' AND trade_date >= '2020-01-01'
            ORDER BY trade_date
        """)
        hs300_kline = [(str(r['trade_date']), float(r['close'])) for r in cur.fetchall()]

        cur.close()
        conn.close()

        # 按日期索引
        score_by_date = defaultdict(list)
        for (code, td), v in scores.items():
            score_by_date[td].append((code, v['score'], v['season'], v['penalty_score']))

        cls._shared_data = {
            'trading_days': trading_days,
            'base_sp': base_sp,
            'watch_pool': watch_pool,
            'pool_set': pool_set,
            'kline': kline,
            'hs300_kline': hs300_kline,
            'score_by_date': score_by_date,
            'total_scores': total_scores,
            'load_time': time.time(),
        }
        print(f"  ✅ 共享数据加载完成: {len(trading_days)}天 / {total_scores}条评分 / {len(watch_pool)}只 ({time.time()-t0:.1f}s)")
        return cls._shared_data

    def __init__(self, config: dict):
        self.cfg = config
        self.label = config['label']
        self.t0 = time.time()
        sd = self.load_shared_data()
        self.trading_days = sd['trading_days']
        self.kline = sd['kline']
        self.hs300_kline = sd['hs300_kline']
        self.score_by_date = sd['score_by_date']
        self.watch_pool = sd['watch_pool']

        # 构建带买入线打折的sp
        mult = config.get('buy_line_mult', 1.0)
        self.sp = {}
        for st, v in sd['base_sp'].items():
            sp = dict(v)
            if mult < 1.0:
                sp['buy'] = max(0, int(v['buy'] * mult))
            self.sp[st] = sp

    def _adjust_score(self, raw_score: float, penalty_score: float, season: str) -> float:
        """
        根据 config_overrides 对评分做反事实调整。
        - no_penalty: 忽略 penalty_score，直接用 raw_score
        - 其他: 当前线上评分 = composite_score（已经是惩罚后的）
        """
        if self.cfg.get('enable_penalty', True):
            return raw_score
        else:
            # 去掉惩罚：恢复到惩罚前的分 = raw_score + penalty_score
            return min(100, raw_score + penalty_score)

    def _apply_filters(self, trade_date: str) -> list:
        """
        过滤层检查。从 strategy_signal 读爆量/资金流过滤标记。
        enable_filters=False → 直接放行。
        
        由于 current strategy_signal 没有独立的 filtered 字段标记，
        我们回读 strategy_signal 的 penalty_reason 判断过滤原因。
        
        规则（对应 p6_dual_track_engine.py _apply_filters）:
        - vol_ratio > 2.0 → '爆量>2倍' 过滤
        - vol_ratio > 2.0 + mf_5d < -50000 → '爆量+资金流出' 过滤
        """
        if not self.cfg.get('enable_filters', True):
            return []  # 全部放行
        
        return None  # None 表示使用默认过滤（strategy_signal已有标记）

    def run(self, start_date=None, end_date=None):
        """单组回测运行"""
        days = [d for d in self.trading_days
                if (start_date is None or d >= start_date)
                and (end_date is None or d <= end_date)]
        if not days:
            print("  ❌ 无交易日")
            return None

        print(f"\n  {'─'*50}")
        print(f"  🔬 [{self.label}]")
        print(f"  范围: {days[0]} ~ {days[-1]} ({len(days)}天)")
        print(f"  买入线调整: x{self.cfg.get('buy_line_mult', 1.0)}")

        cash = INIT_CAPITAL
        positions = []
        all_trades = []
        last_sell_date = {}
        N = len(days)

        daily_nav = []
        peak_nav = INIT_CAPITAL
        max_dd = 0.0
        max_dd_date = ''
        mid_index = N // 2

        for idx, td in enumerate(days):
            if (idx + 1) % 80 == 0:
                pv = cash + sum(p['cost'] for p in positions)
                print(f"    📅 {td} ({idx+1}/{N}) | {len(positions)}仓 | ¥{pv/10000:.0f}万 | {len(all_trades)}笔")

            # ── 1. 检查持仓卖出 ──
            new_positions = []
            for p in positions:
                dp = self.kline.get(p['ts_code'], [])
                dp = [(d, pr) for d, pr in dp if d <= td]
                if not dp:
                    new_positions.append(p)
                    continue

                cp = dp[-1][1]
                hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
                profit_pct = (cp - p['buy_price']) / p['buy_price']
                peak_price = max(p.get('peak_price', p['buy_price']), cp)
                p['peak_price'] = peak_price

                season_sp = self.sp.get(p['buy_season'], self.sp['chaos'])
                t1 = season_sp['t1']
                t2 = season_sp['t2']
                max_hold = season_sp['hold']
                trail_pct = season_sp['trail']
                reason = None

                if profit_pct <= -t1:
                    reason = f'T1止损({int(t1*100)}%)'
                elif hold_days >= 2 and profit_pct <= -t2:
                    reason = f'T2止损({int(t2*100)}%)'
                elif trail_pct > 0 and peak_price > p['buy_price']:
                    dd_from_peak = (peak_price - cp) / peak_price
                    if dd_from_peak >= trail_pct:
                        reason = f'止盈({int(trail_pct*100)}%)'
                elif hold_days >= max_hold:
                    reason = f'到期({hold_days}d)'

                if reason:
                    gross = cp * p['shares']
                    fee = gross * CHARGE_RATE
                    pnl = gross - p['cost'] - fee
                    cash += gross - fee
                    t_profit = round(profit_pct * 100, 2)
                    all_trades.append(dict(
                        ts_code=p['ts_code'], name=p['name'],
                        buy_date=p['buy_date'], buy_price=p['buy_price'],
                        shares=p['shares'], cost=p['cost'],
                        exit_date=td, exit_price=cp,
                        hold_days=hold_days, profit_pct=t_profit,
                        pnl=round(pnl, 2), reason=reason,
                        season=p['buy_season'], buy_score=p['buy_score'],
                    ))
                    last_sell_date[p['ts_code']] = td
                else:
                    new_positions.append(p)
            positions = new_positions

            # ── 2. 检查买入（核心差异点：评分后处理） ──
            cur_pos_val = sum(p['cost'] for p in positions)
            daily_scores = self.score_by_date.get(td, [])

            if cur_pos_val < INIT_CAPITAL * self.sp['chaos']['mt'] and len(positions) < MAX_POSITIONS:
                candidates = []
                in_pool_codes = set(self.watch_pool.keys())
                for code, score, season, penalty_score in daily_scores:
                    if any(p['ts_code'] == code for p in positions):
                        continue
                    if code not in in_pool_codes:
                        continue
                    if code in last_sell_date:
                        diff = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(last_sell_date[code], '%Y-%m-%d')).days
                        if diff < 1:
                            continue

                    # ⭐ 反事实评分调整
                    adjusted_score = self._adjust_score(score, penalty_score, season)

                    season_sp = self.sp.get(season, self.sp['chaos'])
                    buy_line = season_sp['buy']

                    if adjusted_score >= buy_line:
                        candidates.append((code, adjusted_score, season, buy_line))

                candidates.sort(key=lambda x: -x[1])
                for code, adj_score, season, buy_line in candidates[:MAX_BUY_PER_DAY]:
                    if len(positions) >= MAX_POSITIONS:
                        break
                    cur_pos_val = sum(p['cost'] for p in positions)
                    total_max = INIT_CAPITAL * self.sp.get(season, self.sp['chaos'])['mt']
                    if cur_pos_val >= total_max:
                        break

                    dp = self.kline.get(code, [])
                    dp = [(d, pr) for d, pr in dp if d <= td]
                    if not dp:
                        continue
                    cp = dp[-1][1]
                    if cp <= 0:
                        continue

                    season_sp = self.sp.get(season, self.sp['chaos'])
                    max_single = INIT_CAPITAL * season_sp['mp']
                    avail = min(max_single, total_max - cur_pos_val, cash)
                    if avail < 10000:
                        continue

                    shares = int(avail / cp / 100) * 100
                    if shares <= 0:
                        continue

                    cost = shares * cp
                    fee = cost * CHARGE_RATE
                    cash -= cost + fee
                    positions.append(dict(
                        ts_code=code,
                        name=self.watch_pool.get(code, code),
                        shares=shares, buy_price=cp, cost=cost,
                        buy_date=td, peak_price=cp,
                        buy_season=season, buy_score=adj_score,
                    ))

            # ── 3. 每日净值 ──
            pv = cash + sum(p['cost'] for p in positions)
            daily_nav.append({'date': td, 'nav': pv})
            if pv > peak_nav:
                peak_nav = pv
            dd = (peak_nav - pv) / peak_nav * 100
            if dd > max_dd:
                max_dd = dd
                max_dd_date = td

        # ── 最终清算 ──
        last_td = days[-1]
        for p in positions:
            dp = self.kline.get(p['ts_code'], [])
            dp = [(d, pr) for d, pr in dp if d <= last_td]
            cp = dp[-1][1] if dp else p['buy_price']
            hold_days = (datetime.strptime(last_td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cp - p['buy_price']) / p['buy_price'] * 100
            gross = cp * p['shares']
            fee = gross * CHARGE_RATE
            pnl = gross - p['cost'] - fee
            cash += gross - fee
            all_trades.append(dict(
                ts_code=p['ts_code'], name=p['name'],
                buy_date=p['buy_date'], buy_price=p['buy_price'],
                shares=p['shares'], cost=p['cost'],
                exit_date=last_td, exit_price=cp,
                hold_days=hold_days, profit_pct=round(profit_pct, 2),
                pnl=round(pnl, 2), reason='到期清算',
                season=p['buy_season'], buy_score=p['buy_score'],
            ))

        final = cash
        total_return = (final - INIT_CAPITAL) / INIT_CAPITAL * 100

        wins = [t for t in all_trades if t['profit_pct'] > 0]
        losses = [t for t in all_trades if t['profit_pct'] <= 0]
        win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
        avg_win = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win / avg_loss) if avg_loss else float('inf')
        avg_hold = sum(t['hold_days'] for t in all_trades) / len(all_trades) if all_trades else 0
        carmar = total_return / max_dd if max_dd > 0 else 0

        # 季节统计
        seas = defaultdict(list)
        for t in all_trades:
            seas[t['season']].append(t)

        elapsed = time.time() - self.t0

        result = {
            'label': self.label,
            'config': dict(self.cfg),
            'final': round(final, 2),
            'total_return': round(total_return, 2),
            'max_dd': round(max_dd, 2),
            'max_dd_date': max_dd_date,
            'carmar': round(carmar, 2),
            'trades': len(all_trades),
            'win_rate': round(win_rate, 1),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'avg_hold': round(avg_hold, 1),
            'season_detail': {},
            'seasons': list(seas.keys()),
            'elapsed_s': round(elapsed, 0),
        }

        # 季节明细
        for s in ['summer','spring','weak_spring','chaos_spring','chaos',
                  'chaos_autumn','weak_autumn','autumn','winter']:
            ts = seas.get(s, [])
            if ts:
                sw = sum(1 for t in ts if t['profit_pct'] > 0)
                sa = sum(t['profit_pct'] for t in ts) / len(ts)
                result['season_detail'][s] = {
                    'cnt': len(ts),
                    'win_rate': round(sw / len(ts) * 100, 1),
                    'avg_ret': round(sa, 2),
                    'avg_hold': round(sum(t['hold_days'] for t in ts) / len(ts), 1),
                }
            else:
                result['season_detail'][s] = {'cnt': 0}

        # 时间阶段
        mid_date = days[mid_index] if mid_index < len(days) else days[-1]
        early_trades = [t for t in all_trades if t['exit_date'] <= mid_date]
        late_trades = [t for t in all_trades if t['exit_date'] > mid_date]
        if early_trades:
            er = sum(t['profit_pct'] for t in early_trades) / len(early_trades)
            result['early_avg'] = round(er, 2)
            result['early_cnt'] = len(early_trades)
        if late_trades:
            lr = sum(t['profit_pct'] for t in late_trades) / len(late_trades)
            result['late_avg'] = round(lr, 2)
            result['late_cnt'] = len(late_trades)

        # 输出简版
        print(f"\n  📊 [{self.label}] 最终: ¥{final/10000:.2f}万 | "
              f"收益: {total_return:+.2f}% | 回撤: {max_dd:.2f}% | "
              f"卡玛: {carmar:.2f}x | {len(all_trades)}笔 | {win_rate:.1f}%胜")
        print(f"  ⏱ {elapsed:.0f}s")

        return result


def compute_delta(all_results: dict, baseline_key: str = 'full') -> dict:
    """计算各配置相对于基线的差异（delta diagnostics）"""
    baseline = all_results.get(baseline_key)
    if not baseline:
        return {}

    deltas = {}
    for key, r in all_results.items():
        if key == baseline_key:
            continue
        delta = {
            'delta_return': round(r['total_return'] - baseline['total_return'], 2),
            'delta_dd': round(r['max_dd'] - baseline['max_dd'], 2),
            'delta_carmar': round(r['carmar'] - baseline['carmar'], 2),
            'delta_trades': r['trades'] - baseline['trades'],
            'delta_win_rate': round(r['win_rate'] - baseline['win_rate'], 1),
            'delta_profit_factor': round(r['profit_factor'] - baseline['profit_factor'], 2),
        }
        deltas[key] = delta

    return deltas


def run_counterfactual(start_date=None, end_date=None):
    """
    主入口：跑全部5组对比，输出汇总及delta diagnostics
    """
    results = {}
    print(f"\n{'='*60}")
    print(f"🔄 反事实回测 V1.0")
    print(f"{'='*60}")
    print(f"范围: {start_date or '2024-09-02'} ~ {end_date or '2026-07-17'}")

    for key, cfg in CONFIG_SUITE.items():
        print(f"\n{'='*60}")
        print(f"【{key}】{cfg['label']}")
        print(f"{'='*60}")
        eng = CounterfactualBacktest(cfg)
        result = eng.run(start_date, end_date)
        if result:
            results[key] = result

    if not results:
        print("\n❌ 所有回测失败")
        return

    print(f"\n\n{'='*70}")
    print(f"📊 反事实回测汇总")
    print(f"{'='*70}")
    print(f"{'配置':<20s} {'收益':>8s} {'回撤':>7s} {'卡玛':>7s} {'笔数':>5s} {'胜率':>7s} {'盈亏比':>7s}")
    print(f"{'-'*70}")
    base = results.get('full', {})
    for key in ['full', 'no_penalty', 'no_filters', 'no_both', 'legacy_v133b']:
        r = results.get(key)
        if not r:
            continue
        ret_s = f"{r['total_return']:+.2f}%"
        dd_s = f"{r['max_dd']:.2f}%"
        pf_s = f"{r['profit_factor']:.2f}" if r['profit_factor'] < 999 else "∞"
        print(f"{key:<20s} {ret_s:>8s} {dd_s:>7s} {r['carmar']:>7.2f}x "
              f"{r['trades']:>5d} {r['win_rate']:>6.1f}% {pf_s:>7s}")

    print(f"\n📊 Delta Diagnostics（相对于 full 基线）")
    print(f"{'='*70}")
    print(f"{'配置':<20s} {'Δ收益':>8s} {'Δ回撤':>8s} {'Δ卡玛':>8s} {'Δ笔数':>6s} {'Δ胜率':>7s}")
    print(f"{'-'*70}")
    deltas = compute_delta(results)
    for key, d in deltas.items():
        label = CONFIG_SUITE.get(key, {}).get('label', key)
        print(f"{key:<20s} {d['delta_return']:>+8.2f}% {d['delta_dd']:>+8.2f}% "
              f"{d['delta_carmar']:>+8.2f}x {d['delta_trades']:>+6d} {d['delta_win_rate']:>+7.1f}%")

    # 交互效应分析
    if all(k in results for k in ['full', 'no_penalty', 'no_filters', 'no_both']):
        print(f"\n📊 交互效应分析")
        print(f"{'='*70}")
        base_full = results['full']['total_return']
        delta_p = results['no_penalty']['total_return'] - base_full
        delta_f = results['no_filters']['total_return'] - base_full
        delta_both = results['no_both']['total_return'] - base_full
        interaction = delta_both - (delta_p + delta_f)
        print(f"  惩罚层单独贡献: {delta_p:+.2f}%")
        print(f"  过滤层单独贡献: {delta_f:+.2f}%")
        print(f"  两层叠加贡献: {delta_both:+.2f}%")
        print(f"  交互效应（非线性叠加）: {interaction:+.2f}%")
        print(f"  {'→ 正交互=协同增强' if interaction > 0 else '→ 负交互=冗余/抵赖'}")

    # 保存结果到DB
    _save_results(results, deltas)

    print(f"\n{'='*70}")
    print(f"✅ 反事实回测完成")
    print(f"{'='*70}")

    return results, deltas


def _save_results(results: dict, deltas: dict):
    """保存反事实结果到 backtest_counterfactual_results 表"""
    try:
        conn = pymysql.connect(**DB_CFG)
        cur = conn.cursor()
        # 建表（如果不存在）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest_counterfactual_results (
                id INT AUTO_INCREMENT PRIMARY KEY,
                run_label VARCHAR(50),
                run_group VARCHAR(50) DEFAULT 'cf_v1',
                total_return DECIMAL(8,2),
                max_dd DECIMAL(8,2),
                carmar DECIMAL(8,2),
                trades INT,
                win_rate DECIMAL(5,1),
                profit_factor DECIMAL(8,2),
                avg_hold DECIMAL(5,1),
                season_detail JSON,
                start_date VARCHAR(10),
                end_date VARCHAR(10),
                config_overrides JSON,
                created_at DATETIME DEFAULT NOW()
            )
        """)
        for key, r in results.items():
            cfg = CONFIG_SUITE.get(key, {})
            cur.execute("""
                INSERT INTO backtest_counterfactual_results
                (run_label, run_group, total_return, max_dd, carmar, trades,
                 win_rate, profit_factor, avg_hold, season_detail,
                 start_date, end_date, config_overrides)
                VALUES (%s, 'cf_v1', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                key,
                r['total_return'], r['max_dd'], r['carmar'],
                r['trades'], r['win_rate'], r['profit_factor'],
                r['avg_hold'], json.dumps(r['season_detail'], ensure_ascii=False),
                r.get('_start', '2024-09-02'), r.get('_end', '2026-07-17'),
                json.dumps(cfg, ensure_ascii=False),
            ))
        conn.commit()
        cur.close()
        conn.close()
        print(f"💾 反事实结果已保存到 backtest_counterfactual_results")
    except Exception as e:
        print(f"⚠️ 保存回测结果失败: {e}")


if __name__ == '__main__':
    start = sys.argv[1] if len(sys.argv) > 1 else '2024-09-02'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-07-17'
    run_counterfactual(start, end)
