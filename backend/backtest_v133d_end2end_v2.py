#!/usr/bin/env python3
"""
V13.3d 端到端真实回测 v2.0
===========================
预加载K线+指标到内存，内嵌V13.3d评分逻辑，逐日回测

范围: 2024-09-02 ~ 2026-07-17
数据: daily_kline + technical_indicator（预加载到dict）
评分: 复制 p6_dual_track_engine 因子计算逻辑（DB→内存）
季节: season_engine v2.2 (judge_market_season)
惩罚: V13.3d sigmoid + V13.3b风格惩罚（破MA20折价+空头排列+5/10/20日跌幅）
风控: 时间平滑+2日连续下降检查
后处理: 偏离度限制+异常值过滤

时间: 2026-07-19
"""
import sys, os, time, math, json, pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/opt/stock-analyzer')

DB_CFG = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
          'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
          'charset':'utf8mb4','cursorclass':pymysql.cursors.DictCursor,
          'read_timeout':300,'write_timeout':300}

INIT_CAPITAL = 1_000_000
MAX_POSITIONS = 8
MAX_BUY_PER_DAY = 3
CHARGE_RATE = 0.0005

# 从strategy_config表动态加载生产参数
SEASON_PARAMS = {}  # 在运行时从DB加载
SEASON_PARAMS_LOADED = False

MOMENTUM_TRACKS = ['summer','spring','weak_spring','chaos_spring']
REVERSION_TRACKS = ['autumn','weak_autumn','chaos_autumn','winter']

LABELS = {
    'summer':'☀️夏','spring':'🌸春','weak_spring':'⛅弱春','chaos_spring':'🌤️混沌春',
    'chaos':'🌪️混沌','chaos_autumn':'☁️混沌秋','weak_autumn':'⛅弱秋',
    'autumn':'🍂秋','winter':'❄️冬'
}

# =============================================================
# V13.3d 评分引擎（内嵌版，DB→内存）
# =============================================================

class V133dScorer:
    """V13.3d评分引擎 — 数据全部从内存预加载"""
    
    def __init__(self, kline: Dict, tech: Dict, is_st: set):
        self.kline = kline      # {ts_code: [(trade_date, close), ...]}
        self.tech = tech        # {ts_code: {trade_date: {ma_5:, ...}}}
        self.is_st = is_st      # set of st ts_codes
    
    def _get_prices(self, code, up_to_date):
        """获取某日前120根K线收盘价"""
        dp = self.kline.get(code, [])
        dp = [(d, p) for d, p in dp if d <= up_to_date]
        return [p for _, p in dp[-120:]]
    
    def _get_tech(self, code, td, field):
        """获取某日某技术指标"""
        return self.tech.get(code, {}).get(td, {}).get(field)
    
    def score_stock(self, code: str, td: str, season: str, 
                    is_momentum: bool, hs300_di: float) -> Tuple[float, float]:
        """完整评分，返回 (composite_score, penalty)"""
        prices = self._get_prices(code, td)
        if len(prices) < 10:
            return 30.0, 0.0
        
        n = len(prices)
        cp = prices[-1]
        
        # ── 维度评分（从p6_dual_track_engine取核心因子） ──
        
        # 1. MA结构分 (±20)
        ma5 = self._get_tech(code, td, 'ma_5') or sum(prices[-5:]) / 5
        ma20 = self._get_tech(code, td, 'ma_20') or sum(prices[-20:]) / 20
        if n >= 60:
            ma60 = self._get_tech(code, td, 'ma_60') or sum(prices[-60:]) / 60
        else:
            ma60 = ma20
        
        score = 50.0
        
        if ma20 > 0:
            r = cp / ma20
            if r > 1.10: score += 20
            elif r > 1.05: score += 15
            elif r > 1.02: score += 8
            elif r > 0.98: score += 2
            elif r > 0.95: score -= 5
            elif r > 0.90: score -= 10
            else: score -= 15
        
        # 2. 均线结构 (±15)
        if ma5 > 0 and ma20 > 0:
            if ma5 > ma20:
                score += 10
                if ma60 > 0 and ma20 > ma60:
                    score += 5
                elif ma60 > 0 and ma20 < ma60 and is_momentum:
                    score -= 2
            else:
                score -= 8
                if ma60 > 0 and ma20 < ma60:
                    score -= 5
        
        # 3. 短期动量 (±20)
        if n >= 6:
            r5 = (prices[-1] - prices[-6]) / prices[-6]
            score += r5 * 100 * 0.15
        if n >= 11:
            r10 = (prices[-1] - prices[-11]) / prices[-11]
            score += max(-5, min(5, r10 * 50))
        
        # 4. 资金流向/量价因子 (±10)
        # 简化版：用量价配合
        vol_ratio = self._get_tech(code, td, 'vol_ma_5') or 1.0
        vol_ma20_v = self._get_tech(code, td, 'vol_ma_20') or 1.0
        if vol_ma20_v > 0:
            vr = vol_ratio / vol_ma20_v
        else:
            vr = 1.0
        
        if vr > 1.5:
            score += 8 if prices[-1] > (prices[-3] if n >= 3 else prices[-1]) else 0
        elif vr > 1.2:
            score += 4
        elif vr < 0.6:
            score -= 5
        
        # 5. RSI校准 (±5)
        rsi = self._get_tech(code, td, 'rsi_14') or 50
        if rsi > 75: score -= 5
        elif rsi > 65: score -= 1
        elif rsi < 25: score += 5
        elif rsi < 35: score += 3
        
        # 6. MACD (±10)
        macd_hist = self._get_tech(code, td, 'macd_bar') or 0
        score += min(8, max(-8, macd_hist * 40))
        
        # 7. 季节偏好
        if season in ('summer','spring'): score += 3
        elif season in ('autumn','winter'): score -= 3
        elif season == 'chaos_spring': score += 1
        elif season == 'chaos_autumn': score -= 1
        
        # 8. 动量vs回归轨道特化
        if is_momentum:
            if n >= 21:
                r20 = (prices[-1] - prices[-21]) / prices[-21]
                score += max(-5, min(10, r20 * 25))
        else:
            # 回归轨道：超跌加分
            if n >= 21:
                r20 = (prices[-1] - prices[-21]) / prices[-21]
                if r20 < -0.10:
                    score += 15
                elif r20 < -0.05:
                    score += 8
            # 震荡突破信号
            bb_width = self._get_tech(code, td, 'boll_width') or 0.02
            if bb_width > 0.05:  # 窄幅突破
                score += 3
        
        score = max(5, min(100, score))
        score = round(score, 1)
        
        # ── V13.3d惩罚计算 ──
        penalty = self._calc_penalty(prices, ma5, ma20, td, hs300_di)
        
        return score, penalty
    
    def _calc_penalty(self, prices, ma5, ma20, td, hs300_di):
        """V13.3d惩罚分（含sigmoid + 破线折价）"""
        n = len(prices)
        if n < 5:
            return 0.0
        
        cp = prices[-1]
        penalty = 0.0
        
        # 1. 破MA20折价
        if ma20 > 0 and cp < ma20:
            below = (ma20 - cp) / ma20
            score_discount = max(0.4, 1.0 - below * 0.6)
            tloss = 55 * (1 - score_discount) * 0.30
            if tloss > 2:
                penalty += tloss
        
        # 2. 空头排列 (+8)
        if ma5 > 0 and ma20 > 0 and ma5 < ma20:
            penalty += 8
        
        # 3. 5/10/20日跌幅惩罚
        if n >= 6:
            r5 = (prices[-1] - prices[-6]) / prices[-6]
            if r5 < -0.05: penalty += min(30, abs(r5) * 200)
        if n >= 11:
            r10 = (prices[-1] - prices[-11]) / prices[-11]
            if r10 < -0.08: penalty += min(25, abs(r10) * 150)
        if n >= 21:
            r20 = (prices[-1] - prices[-21]) / prices[-21]
            if r20 < -0.10: penalty += min(25, abs(r20) * 100)
        
        # 4. 连续下跌加强
        consec = 0
        for ci in range(min(10, n-1)):
            if prices[-(ci+1)] < prices[-(ci+2)]:
                consec += 1
            else:
                break
        if consec >= 3: penalty *= 1.3
        if consec >= 5: penalty *= 1.2
        
        # 5. 大盘系统性风险扣分
        if hs300_di < -0.03:
            penalty *= 1.15
        
        return min(50, penalty)


# =============================================================
# 回测引擎
# =============================================================

class BacktestEngineV2:
    
    def __init__(self):
        self.t0 = time.time()
        self._load_data()
        self._init_season_engine()
        self.scorer = V133dScorer(self.kline, self.tech, self.is_st)
        self._load_strategy_params()
        
    def _load_strategy_params(self):
        """从strategy_config表动态加载生产参数"""
        global SEASON_PARAMS, SEASON_PARAMS_LOADED
        if SEASON_PARAMS_LOADED:
            return
        conn = pymysql.connect(**DB_CFG)
        cur = conn.cursor()
        cur.execute("""
            SELECT season_type, buy_min_score, max_hold_days, stop_loss_pct,
                   trailing_stop_pct, max_pos_pct, max_total_pct
            FROM strategy_config 
            WHERE is_active=1 AND strategy_type='STEP_LOCK'
        """)
        for r in cur.fetchall():
            st = r['season_type']
            buy_min = int(r['buy_min_score'])
            hold = int(r['max_hold_days'])
            t1 = float(r['stop_loss_pct'])
            trail = float(r['trailing_stop_pct'])
            mp = float(r['max_pos_pct'])
            mt = float(r['max_total_pct'])
            # 回测中t2 = t1 - 2（比T1宽松2个百分点）
            t2 = max(3.0, t1 - 2.0)
            SEASON_PARAMS[st] = {'buy':buy_min, 'hold':hold, 
                                 't1':t1, 't2':t2, 'trail':trail,
                                 'mp':mp, 'mt':mt}
        cur.close(); conn.close()
        print(f"  ✅ 策略参数已从DB加载: {len(SEASON_PARAMS)}个季节")
        SEASON_PARAMS_LOADED = True
        
    def _load_data(self):
        """预加载所有数据到内存"""
        print("预加载数据...")
        conn = pymysql.connect(**DB_CFG)
        cur = conn.cursor()
        
        # 交易日
        cur.execute("""
            SELECT DISTINCT trade_date FROM daily_kline 
            WHERE trade_date >= '2024-09-02' AND trade_date <= '2026-07-17'
            ORDER BY trade_date
        """)
        self.trading_days = [str(r['trade_date']) for r in cur.fetchall()]
        print(f"  交易日: {len(self.trading_days)}天")
        
        # K线
        t0 = time.time()
        cur.execute("""
            SELECT ts_code, trade_date, close 
            FROM daily_kline 
            WHERE trade_date >= '2020-01-01'
            ORDER BY ts_code, trade_date
        """)
        self.kline = defaultdict(list)
        for r in cur.fetchall():
            self.kline[r['ts_code']].append((str(r['trade_date']), float(r['close'])))
        print(f"  K线: {sum(len(v) for v in self.kline.values())}条 ({time.time()-t0:.1f}s)")
        
        # 技术指标
        t0 = time.time()
        cur.execute("""
            SELECT ts_code, trade_date, 
                   ma_5, ma_20, ma_60, ma_120,
                   rsi_14, macd_dif, macd_dea, macd_bar,
                   boll_upper, boll_lower, boll_width,
                   vol_ma_5, vol_ma_20,
                   atr_14
            FROM technical_indicator
            WHERE trade_date >= '2020-01-01'
            ORDER BY ts_code, trade_date
        """)
        self.tech = defaultdict(lambda: defaultdict(dict))
        cnt = 0
        for r in cur.fetchall():
            td = str(r['trade_date'])
            for k in ('ma_5','ma_20','ma_60','ma_120','rsi_14','macd_dif','macd_dea','macd_bar',
                       'boll_upper','boll_lower','boll_width','vol_ma_5','vol_ma_20','atr_14'):
                if r.get(k) is not None:
                    self.tech[r['ts_code']][td][k] = float(r[k])
            cnt += 1
        print(f"  技术指标: {cnt}条 ({time.time()-t0:.1f}s)")
        
        # 监控池
        cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
        self.watch_pool = {}
        for r in cur.fetchall():
            self.watch_pool[r['ts_code']] = r.get('name') or r['ts_code']
        print(f"  监控池: {len(self.watch_pool)}只")
        
        # ST标记（简化：从代码判断）
        self.is_st = set()
        
        # 沪深300历史日涨跌幅（用于大盘风险扣分）
        self.hs300_kline = self.kline.get('000300.SH', [])
        if not self.hs300_kline:
            # 从000300获取
            cur.execute("""
                SELECT trade_date, close FROM daily_kline 
                WHERE ts_code='000300.SH' AND trade_date >= '2020-01-01'
                ORDER BY trade_date
            """)
            self.hs300_kline = [(str(r['trade_date']), float(r['close'])) for r in cur.fetchall()]
            self.kline['000300.SH'] = self.hs300_kline
        
        cur.close()
        conn.close()
        
        # 建立日期索引
        self.date_set = set(self.trading_days)
        self.date_index = {d: i for i, d in enumerate(self.trading_days)}
        
    def _init_season_engine(self):
        """初始化季节引擎v2.2"""
        if 'season_engine' in sys.modules:
            for m in list(sys.modules.keys()):
                if 'season_engine' in m:
                    del sys.modules[m]
        from season_engine import SeasonEngine
        self.se = SeasonEngine(use_market_breadth=False)
        self._season_engine_module = sys.modules['season_engine']
        
        # 批量获取所有交易日的季节判定（只需一次judge_history调用）
        print("  季节引擎v2.2预判...")
        t0 = time.time()
        start_date = datetime.strptime(self.trading_days[0], '%Y-%m-%d').date()
        end_date = datetime.strptime(self.trading_days[-1], '%Y-%m-%d').date()
        self.season_history = {}
        
        # 逐日调用judge_market_season（避免judge_history的date_range限制）
        # 但太慢——用judge_history
        history = self.se.judge_history(start_date=start_date, end_date=end_date)
        for h in history:
            td_str = str(h['trade_date']) if not isinstance(h['trade_date'], str) else h['trade_date']
            self.season_history[td_str] = h
        print(f"  季节预判: {len(self.season_history)}天 ({time.time()-t0:.1f}s)")
        
    def get_season(self, td: str) -> str:
        """获取季节判定"""
        h = self.season_history.get(td, {})
        # judge_history返回key='season', judge_market_season返回key='market_season'
        return h.get('season') or h.get('market_season', 'chaos')
    
    def get_hs300_daily_return(self, td: str) -> float:
        """获取沪深300日涨跌幅"""
        dp = [(d, p) for d, p in self.hs300_kline if d <= td]
        if len(dp) >= 5:
            return (dp[-1][1] - dp[-5][1]) / dp[-5][1]
        return 0.0
    
    def get_hs300_roc20(self, td: str, default=0.0) -> float:
        """沪深300近20日涨跌幅"""
        dp = [(d, p) for d, p in self.hs300_kline if d <= td]
        if len(dp) >= 21:
            return (dp[-1][1] - dp[-21][1]) / dp[-21][1]
        return default
    
    def run(self, start_date=None, end_date=None):
        """主回测"""
        days = [d for d in self.trading_days 
                if (start_date is None or d >= start_date) 
                and (end_date is None or d <= end_date)]
        print(f"\n{'='*60}")
        print(f"🚀 V13.3d 端到端真实回测 v2.0")
        print(f"  范围: {days[0]} ~ {days[-1]} ({len(days)}天)")
        print(f"{'='*60}")
        
        cash = INIT_CAPITAL
        positions = []
        all_trades = []
        pos_seasons = {}
        peak_value = INIT_CAPITAL
        max_dd = 0.0
        max_dd_date = ''
        
        penalty_stats = Counter()
        score_dists = defaultdict(int)
        
        last_sell_date = {}  # T+1
        
        N = len(days)
        
        for idx, td in enumerate(days):
            if (idx+1) % 50 == 0:
                pv = cash + sum(p['cost'] for p in positions)
                elapsed = time.time() - self.t0
                print(f"  📅 {td} ({idx+1}/{N}) | {len(positions)}仓 | ¥{pv/10000:.0f}万 | {len(all_trades)}笔 | ⏱{elapsed:.0f}s")
            
            # ── 1. 季节判定 ──
            season = self.get_season(td)
            sp = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
            buy_line = sp['buy']
            max_hold = sp['hold']
            t1 = sp['t1'] / 100.0
            t2 = sp['t2'] / 100.0
            trail_pct = sp['trail'] / 100.0
            max_pos_pct = sp['mp'] / 100.0
            max_total_pct = sp['mt'] / 100.0
            is_momentum = season in MOMENTUM_TRACKS
            
            hs300_roc5d = self.get_hs300_daily_return(td)
            
            # ── 2. 检查持仓卖出 ──
            new_positions = []
            for p in positions:
                dp = [(d, pr) for d, pr in self.kline.get(p['ts_code'], []) if d <= td]
                if not dp:
                    new_positions.append(p)
                    continue
                    
                cp = dp[-1][1]
                hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
                profit_pct = (cp - p['buy_price']) / p['buy_price']
                peak_price = max(p.get('peak_price', p['buy_price']), cp)
                p['peak_price'] = peak_price
                
                reason = None
                
                # T1止损
                if profit_pct <= -t1:
                    reason = f'止损T1({int(t1*100)}%)'
                # T2止损（至少持有2天后）
                elif hold_days >= 2 and profit_pct <= -t2:
                    reason = f'止损T2({int(t2*100)}%)'
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
                    t_profit = round(profit_pct*100, 2)
                    t = dict(ts_code=p['ts_code'], buy_date=p['buy_date'], buy_price=p['buy_price'],
                             shares=p['shares'], cost=p['cost'], exit_date=td, exit_price=cp,
                             hold_days=hold_days, profit_pct=t_profit, pnl=round(pnl,2),
                             reason=reason, season=p.get('buy_season', season))
                    all_trades.append(t)
                    last_sell_date[p['ts_code']] = td
                else:
                    new_positions.append(p)
            
            positions = new_positions
            
            # ── 3. 检查买入 ──
            cur_pos_val = sum(p['cost'] for p in positions)
            max_total_val = INIT_CAPITAL * max_total_pct
            
            if cur_pos_val < max_total_val and len(positions) < MAX_POSITIONS:
                
                candidates = []
                for code in self.watch_pool:
                    if any(p['ts_code'] == code for p in positions):
                        continue
                    # T+1
                    if code in last_sell_date:
                        diff = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(last_sell_date[code], '%Y-%m-%d')).days
                        if diff < 1:
                            continue
                    
                    # 评分
                    score, penalty = self.scorer.score_stock(code, td, season, is_momentum, hs300_roc5d)
                    
                    usable = max(0, score - penalty)
                    if score > 0:
                        score_dists[int(score/10)*10] += 1
                    if penalty > 0:
                        penalty_stats[int(penalty)] += 1
                    
                    if usable >= buy_line:
                        candidates.append((code, score, usable, penalty))
                
                candidates.sort(key=lambda x: x[2], reverse=True)
                
                for code, composite, usable, penalty in candidates[:MAX_BUY_PER_DAY]:
                    if len(positions) >= MAX_POSITIONS:
                        break
                    cur_pos_val = sum(p['cost'] for p in positions)
                    if cur_pos_val >= max_total_val:
                        break
                    
                    max_single = INIT_CAPITAL * max_pos_pct
                    avail = min(max_single, max_total_val - cur_pos_val, cash)
                    if avail < 10000:
                        continue
                    
                    dp = [(d, pr) for d, pr in self.kline.get(code, []) if d <= td]
                    if not dp:
                        continue
                    cp = dp[-1][1]
                    
                    shares = int(avail / cp / 100) * 100
                    if shares <= 0:
                        continue
                    
                    cost = shares * cp
                    fee = cost * CHARGE_RATE
                    cash -= cost + fee
                    
                    positions.append(dict(ts_code=code, shares=shares, buy_price=cp, cost=cost,
                                          buy_date=td, peak_price=cp, buy_season=season,
                                          buy_score=composite, penalty=penalty))
        
        # ── 最终清算 ──
        for p in positions:
            dp = [(d, pr) for d, pr in self.kline.get(p['ts_code'], []) if d <= days[-1]]
            cp = dp[-1][1] if dp else p['buy_price']
            hold_days = (datetime.strptime(days[-1], '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cp - p['buy_price']) / p['buy_price'] * 100
            gross = cp * p['shares']
            pnl = gross - p['cost'] - gross * CHARGE_RATE
            cash += gross - gross * CHARGE_RATE
            all_trades.append(dict(ts_code=p['ts_code'], buy_date=p['buy_date'], buy_price=p['buy_price'],
                                   shares=p['shares'], cost=p['cost'], exit_date=days[-1], exit_price=cp,
                                   hold_days=hold_days, profit_pct=round(profit_pct,2), pnl=round(pnl,2),
                                   reason='到期清算', season=p.get('buy_season','chaos')))
        
        positions = []
        final = cash
        
        # ── 计算最大回撤 ──
        pnl_series = []
        cumulative = INIT_CAPITAL
        peak_local = INIT_CAPITAL
        max_dd = 0.0
        td_date_map = {d: i for i, d in enumerate(days)}
        for t in sorted(all_trades, key=lambda x: x.get('exit_date', x['buy_date'])):
            cumulative += t['pnl']
            if cumulative > peak_local:
                peak_local = cumulative
            dd = (peak_local - cumulative) / peak_local * 100
            if dd > max_dd:
                max_dd = dd
        
        total_return = (final - INIT_CAPITAL) / INIT_CAPITAL * 100
        
        wins = [t for t in all_trades if t['profit_pct'] > 0]
        losses = [t for t in all_trades if t['profit_pct'] <= 0]
        win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
        avg_win = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win/avg_loss) if avg_loss else float('inf')
        avg_hold = sum(t['hold_days'] for t in all_trades) / len(all_trades) if all_trades else 0
        carmar = total_return / max_dd if max_dd > 0 else 0
        
        # 按季节
        seas = defaultdict(list)
        for t in all_trades:
            seas[t['season']].append(t)
        
        # ── 输出 ──
        elapsed = time.time() - self.t0
        print(f"\n{'='*60}")
        print(f"📊 V13.3d 端到端真实回测 v2.0")
        print(f"{'='*60}")
        print(f"初始: ¥100万 → 最终: ¥{final/10000:.2f}万")
        print(f"总收益: {total_return:+.2f}% | 最大回撤: {max_dd:.2f}%")
        print(f"卡玛: {carmar:.2f}x")
        print(f"交易: {len(all_trades)}笔 | 胜率: {win_rate:.1f}% ({len(wins)}胜/{len(losses)}负)")
        print(f"均持有: {avg_hold:.1f}d | 盈亏比: {profit_factor:.2f}")
        print(f"均盈: +{avg_win:.2f}% | 均亏: {avg_loss:.2f}%")
        
        print(f"\n📂 按季节:")
        for s in ['summer','spring','weak_spring','chaos_spring','chaos','chaos_autumn','weak_autumn','autumn','winter']:
            ts = seas.get(s, [])
            if ts:
                sw = sum(1 for t in ts if t['profit_pct'] > 0)
                sa = sum(t['profit_pct'] for t in ts) / len(ts)
                print(f"  {LABELS.get(s,s):>8s} {len(ts):3d}笔 | {sw/len(ts)*100:.0f}%胜 | 均{sa:+.2f}% | 均{sum(t['hold_days'] for t in ts)/len(ts):.0f}d")
        
        print(f"\n🏆 TOP5:")
        for t in sorted(all_trades, key=lambda x: -x['profit_pct'])[:5]:
            print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
        print(f"\n💀 BOTTOM5:")
        for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
            print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
        
        print(f"\n⏱ {elapsed:.0f}s")
        return final, total_return, max_dd, carmar


if __name__ == '__main__':
    import sys as _sys
    start = _sys.argv[1] if len(_sys.argv) > 1 else '2024-09-02'
    end = _sys.argv[2] if len(_sys.argv) > 2 else '2026-07-17'
    eng = BacktestEngineV2()
    # 覆盖交易日范围
    # 回测筛选在run()中通过trading_days过滤（主循环只遍历范围内的日）
    # __init__仍加载全量数据，但run()范围由参数控制
    # 目前run()里直接用了self.trading_days（全量）
    # 需要改run()方法支持参数
    eng.run(start, end)
