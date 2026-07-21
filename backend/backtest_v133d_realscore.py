#!/usr/bin/env python3
"""
V13.3d 真实评分回测 v3.0
===========================
直接从 strategy_signal 表读取 composite_score + season，不内嵌评分逻辑。

数据: daily_kline（行情）、strategy_signal（评分+季节）
策略: 从 strategy_config 表动态加载（买入线/止损/持有期/仓位）
交易: 模拟真实交易（T+1、多仓、阶梯止损、持仓上限）

用法:
  python3 backtest_v133d_realscore.py [start_date] [end_date]
  e.g., python3 backtest_v133d_realscore.py 2024-09-02 2026-07-17

时间: 2026-07-20
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


class BacktestEngineRealScore:

    def __init__(self):
        self.t0 = time.time()
        self._load_data()
        
    def _load_data(self):
        """预加载所有数据到内存"""
        conn = pymysql.connect(**DB_CFG)
        cur = conn.cursor()
        
        # ── 交易日 ──
        print("加载数据...")
        cur.execute("""
            SELECT DISTINCT trade_date FROM strategy_signal
            WHERE trade_date >= '2024-09-02' AND trade_date <= '2026-07-17'
            ORDER BY trade_date
        """)
        self.trading_days = [str(r['trade_date']) for r in cur.fetchall()]
        print(f"  交易日: {len(self.trading_days)}天")
        self.date_set = set(self.trading_days)
        self.date_index = {d: i for i, d in enumerate(self.trading_days)}

        # ── 策略参数（从 strategy_config 表） ──
        print("  加载策略参数...")
        cur.execute("""
            SELECT season_type, buy_min_score, max_hold_days,
                   stop_loss_pct, trailing_stop_pct,
                   max_pos_pct, max_total_pct
            FROM strategy_config
            WHERE is_active = 1 AND strategy_type = 'STEP_LOCK'
        """)
        self.sp = {}
        for r in cur.fetchall():
            st = r['season_type']
            self.sp[st] = {
                'buy': int(r['buy_min_score']),
                'hold': int(r['max_hold_days']),
                't1': float(r['stop_loss_pct']) / 100.0,
                't2': max(0.03, float(r['stop_loss_pct']) / 100.0 - 0.02),
                'trail': float(r['trailing_stop_pct']) / 100.0,
                'mp': float(r['max_pos_pct']) / 100.0,
                'mt': float(r['max_total_pct']) / 100.0,
            }
        print(f"  策略参数: {len(self.sp)}个季节")
        
        # ── 监控池 ──
        cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
        self.watch_pool = {}
        for r in cur.fetchall():
            self.watch_pool[r['ts_code']] = r.get('name') or r['ts_code']
        pool_codes = list(self.watch_pool.keys())
        print(f"  监控池: {len(self.watch_pool)}只")
        pool_set = set(pool_codes)

        # ── 评分数据（核心！从strategy_signal表读） ──
        # 结构: scores[(ts_code, trade_date)] = {composite_score, calibrated_score, season}
        print("  加载评分数据...")
        t0 = time.time()
        # 分块读取避免内存爆炸（但其实378K行并不多）
        # 按日期范围分批以减少内存压力
        self.scores = {}  
        chunk_size = 20000
        offset = 0
        total_scores = 0
        while True:
            cur.execute("""
                SELECT ts_code, trade_date, composite_score, calibrated_score, season
                FROM strategy_signal
                WHERE trade_date BETWEEN '2024-09-02' AND '2026-07-17'
                LIMIT %s OFFSET %s
            """, (chunk_size, offset))
            rows = cur.fetchall()
            if not rows:
                break
            for r in rows:
                key = (r['ts_code'], str(r['trade_date']))
                self.scores[key] = {
                    'score': float(r['composite_score']) if r['composite_score'] is not None else 50.0,
                    'cal': float(r['calibrated_score']) if r['calibrated_score'] is not None else None,
                    'season': r['season'] or 'chaos',
                }
            total_scores += len(rows)
            offset += chunk_size
        print(f"  评分记录: {total_scores}条 ({time.time()-t0:.1f}s)")

        # ── K线数据（用于行情） ──
        print("  加载K线...")
        t0 = time.time()
        # 只加载监控池 + 持仓可能要用的close价格
        all_codes = list(pool_set)
        placeholders = ','.join(['%s'] * len(all_codes))
        sql = f"""
            SELECT ts_code, trade_date, close
            FROM daily_kline
            WHERE ts_code IN ({placeholders})
              AND trade_date >= '2020-01-01'
            ORDER BY ts_code, trade_date
        """
        cur.execute(sql, all_codes)
        self.kline = defaultdict(list)
        for r in cur.fetchall():
            self.kline[r['ts_code']].append((str(r['trade_date']), float(r['close'])))
        print(f"  K线: {sum(len(v) for v in self.kline.values())}条 ({time.time()-t0:.1f}s)")

        # 沪深300用于大盘参考（保留）
        self.hs300_dates = set()
        cur.execute("""
            SELECT ts_code, trade_date, close FROM daily_kline
            WHERE ts_code = '000300.SH' AND trade_date >= '2020-01-01'
            ORDER BY trade_date
        """)
        self.hs300_kline = [(str(r['trade_date']), float(r['close'])) for r in cur.fetchall()]
        self.hs300_date_set = set(d for d, _ in self.hs300_kline)
        print(f"  沪深300: {len(self.hs300_kline)}天")

        cur.close()
        conn.close()

        # 按交易日期组织评分索引（便于每日快速查询）
        # score_by_date[trade_date] = [(ts_code, score, season), ...]
        print("  构建评分索引...")
        t0 = time.time()
        self.score_by_date = defaultdict(list)
        for (code, td), v in self.scores.items():
            self.score_by_date[td].append((code, v['score'], v['season']))
        print(f"  评分索引: {len(self.score_by_date)}天 ({time.time()-t0:.1f}s)")

    def get_hs300_daily_return(self, td: str, days: int = 5) -> float:
        """沪深300近N日涨跌幅"""
        dp = [(d, p) for d, p in self.hs300_kline if d <= td]
        if len(dp) >= days + 1:
            return (dp[-1][1] - dp[-(days+1)][1]) / dp[-(days+1)][1]
        return 0.0

    def run(self, start_date=None, end_date=None):
        """主回测"""
        days = [d for d in self.trading_days
                if (start_date is None or d >= start_date)
                and (end_date is None or d <= end_date)]
        if not days:
            print("❌ 没有交易日数据")
            return

        print(f"\n{'='*60}")
        print(f"🚀 V13.3d 真实评分回测 v3.0")
        print(f"  范围: {days[0]} ~ {days[-1]} ({len(days)}天)")
        print(f"  评分源: strategy_signal (P6双轨引擎)")
        print(f"{'='*60}")

        cash = INIT_CAPITAL
        positions = []
        all_trades = []
        last_sell_date = {}
        N = len(days)

        # 每日净值跟踪（计算真实回撤）
        daily_nav = []
        peak_nav = INIT_CAPITAL
        max_dd = 0.0
        max_dd_date = ''

        # 回测阶段标记（用于统计中间/末期效果）
        mid_index = N // 2

        for idx, td in enumerate(days):
            if (idx + 1) % 50 == 0:
                pv = cash + sum(p['cost'] for p in positions)
                elapsed = time.time() - self.t0
                print(f"  📅 {td} ({idx+1}/{N}) | {len(positions)}仓 | ¥{pv/10000:.0f}万 | {len(all_trades)}笔 | ⏱{elapsed:.0f}s")

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

                # T1止损
                if profit_pct <= -t1:
                    reason = f'T1止损({int(t1*100)}%)'
                # T2止损（至少持有2天）
                elif hold_days >= 2 and profit_pct <= -t2:
                    reason = f'T2止损({int(t2*100)}%)'
                # 移动止盈
                elif trail_pct > 0 and peak_price > p['buy_price']:
                    dd_from_peak = (peak_price - cp) / peak_price
                    if dd_from_peak >= trail_pct:
                        reason = f'止盈({int(trail_pct*100)}%)'
                # 持有上限
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

            # ── 2. 检查买入 ──
            cur_pos_val = sum(p['cost'] for p in positions)
            # 获取本日评分
            daily_scores = self.score_by_date.get(td, [])

            if cur_pos_val < INIT_CAPITAL * self.sp['chaos']['mt'] and len(positions) < MAX_POSITIONS:
                # 过滤已有持仓/T+1/不在池中
                candidates = []
                in_pool_codes = set(self.watch_pool.keys())
                for code, score, season in daily_scores:
                    # 排除已持仓
                    if any(p['ts_code'] == code for p in positions):
                        continue
                    # 排除不在监控池的票
                    if code not in in_pool_codes:
                        continue
                    # T+1
                    if code in last_sell_date:
                        diff = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(last_sell_date[code], '%Y-%m-%d')).days
                        if diff < 1:
                            continue

                    season_sp = self.sp.get(season, self.sp['chaos'])
                    buy_line = season_sp['buy']

                    if score >= buy_line:
                        candidates.append((code, score, season, buy_line))

                # 按评分降序排列
                candidates.sort(key=lambda x: -x[1])

                for code, score, season, buy_line in candidates[:MAX_BUY_PER_DAY]:
                    if len(positions) >= MAX_POSITIONS:
                        break
                    cur_pos_val = sum(p['cost'] for p in positions)
                    total_max = INIT_CAPITAL * self.sp.get(season, self.sp['chaos'])['mt']
                    if cur_pos_val >= total_max:
                        break

                    # 找当日行情
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
                        buy_season=season, buy_score=score,
                    ))

            # ── 3. 记录每日净值 ──
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

        # 按季节统计
        seas = defaultdict(list)
        buy_line_hits = defaultdict(int)
        buy_line_trades = defaultdict(int)
        for t in all_trades:
            seas[t['season']].append(t)
            buy_line_hits[t['season']] += 1

        # 也统计每季每天有多少候选
        season_candidate_days = defaultdict(int)
        for td in days:
            td_season_counts = defaultdict(int)
            for code, score, season in self.score_by_date.get(td, []):
                if code in self.watch_pool:
                    ssp = self.sp.get(season, self.sp['chaos'])
                    if score >= ssp['buy']:
                        td_season_counts[season] += 1
            for s, cnt in td_season_counts.items():
                season_candidate_days[s] = max(season_candidate_days[s], cnt)

        # ── 输出 ──
        elapsed = time.time() - self.t0
        print(f"\n{'='*60}")
        print(f"📊 V13.3d 真实评分回测 v3.0")
        print(f"{'='*60}")
        print(f"初始: ¥100万 → 最终: ¥{final/10000:.2f}万")
        print(f"总收益: {total_return:+.2f}% | 最大回撤: {max_dd:.2f}% ({max_dd_date})")
        print(f"卡玛: {carmar:.2f}x")
        print(f"交易: {len(all_trades)}笔 | 胜率: {win_rate:.1f}% ({len(wins)}胜/{len(losses)}负)")
        print(f"均持有: {avg_hold:.1f}d | 盈亏比: {profit_factor:.2f}")
        print(f"均盈: +{avg_win:.2f}% | 均亏: {avg_loss:.2f}%")
        print(f"累计净值: ¥{final/10000:.2f}万 (¥100万起步)")

        print(f"\n📂 按季节:")
        for s in ['summer','spring','weak_spring','chaos_spring','chaos','chaos_autumn','weak_autumn','autumn','winter']:
            ts = seas.get(s, [])
            if ts:
                sw = sum(1 for t in ts if t['profit_pct'] > 0)
                sa = sum(t['profit_pct'] for t in ts) / len(ts)
                sp = self.sp.get(s, {})
                bl = sp.get('buy', '?')
                print(f"  {LABELS.get(s, s):>8s} {len(ts):3d}笔 | {sw/len(ts)*100:.0f}%胜 | 均{sa:+.2f}% | "
                      f"均{sum(t['hold_days'] for t in ts)/len(ts):.0f}d | 买入线{bl}")

        # 没交易但存在的季节
        active_seasons = set(self.sp.keys())
        traded_seasons = set(seas.keys())
        for s in sorted(active_seasons - traded_seasons):
            sp = self.sp.get(s, {})
            bl = sp.get('buy', '?')
            print(f"  {LABELS.get(s, s):>8s} 0笔 ❌ | 买入线{bl}")

        print(f"\n🏆 TOP5:")
        for t in sorted(all_trades, key=lambda x: -x['profit_pct'])[:5]:
            print(f"  {t['name'] or t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {LABELS.get(t['season'], t['season'])}")
        print(f"\n💀 BOTTOM5:")
        for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
            print(f"  {t['name'] or t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {LABELS.get(t['season'], t['season'])}")

        print(f"\n📈 按时间阶段:")
        mid_date = days[mid_index]
        early_trades = [t for t in all_trades if t['exit_date'] <= mid_date]
        late_trades = [t for t in all_trades if t['exit_date'] > mid_date]
        if early_trades:
            early_ret = sum(t['profit_pct'] for t in early_trades) / len(early_trades)
            print(f"  前半段 ({days[0]}~{mid_date}): {len(early_trades)}笔 均{early_ret:+.2f}%")
        if late_trades:
            late_ret = sum(t['profit_pct'] for t in late_trades) / len(late_trades)
            print(f"  后半段 ({mid_date}~{days[-1]}): {len(late_trades)}笔 均{late_ret:+.2f}%")

        print(f"\n⏱ {elapsed:.0f}s")

        # 将结果写入 backtest_results 表，便于后续对比
        self._save_result(start_date or days[0], end_date or days[-1],
                          final, total_return, max_dd, carmar,
                          len(all_trades), win_rate, all_trades)

        return final, total_return, max_dd, carmar

    def _save_result(self, start_date, end_date, final, total_return, max_dd, carmar,
                     trades_count, win_rate, all_trades):
        """保存回测结果到DB"""
        try:
            conn = pymysql.connect(**DB_CFG)
            cur = conn.cursor()
            season_detail = defaultdict(list)
            for t in all_trades:
                season_detail[t['season']].append(t['profit_pct'])

            detail_json = {}
            for s, pts in season_detail.items():
                detail_json[s] = {
                    'cnt': len(pts),
                    'avg': round(sum(pts) / len(pts), 2),
                    'wins': sum(1 for p in pts if p > 0),
                }

            cur.execute("""
                INSERT INTO backtest_results 
                (start_date, end_date, version, total_return, max_dd, carmar, 
                 trades_count, win_rate, season_detail, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                start_date, end_date, 'V13.3d-realscore-v3',
                round(total_return, 2), round(max_dd, 2), round(carmar, 2),
                trades_count, round(win_rate, 1), json.dumps(detail_json, ensure_ascii=False),
            ))
            conn.commit()
            cur.close()
            conn.close()
            print(f"\n💾 回测结果已保存到 backtest_results")
        except Exception as e:
            print(f"\n⚠️ 保存回测结果失败: {e}")


if __name__ == '__main__':
    start = sys.argv[1] if len(sys.argv) > 1 else '2024-09-02'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-07-17'
    eng = BacktestEngineRealScore()
    eng.run(start, end)
