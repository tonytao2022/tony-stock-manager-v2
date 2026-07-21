#!/usr/bin/env python3
"""
V13.3d 端到端真实回测 v1.0
===========================
关键区别：不使用strategy_signal历史评分
而是每天用V13.3d引擎（sigmoid惩罚）对全量股票重新评分

时间: 2026-07-19
"""
import sys, os, time, math, json, pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/opt/stock-analyzer')

DB_CFG = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
          'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
          'charset':'utf8mb4','cursorclass':pymysql.cursors.DictCursor}

INIT_CAPITAL = 1_000_000
MAX_POSITIONS = 8
MAX_BUY_PER_DAY = 3
CHARGE_RATE = 0.0005

# ── V13.2最终参数矩阵（复用） ──
SEASON_PARAMS = {
    'summer':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':18.0, 'max_pos':50, 'max_total':50},
    'spring':         {'buy':65, 'hold':30, 't1':12.0, 't2':9.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'weak_spring':    {'buy':68, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':35, 'max_total':40},
    'chaos_spring':   {'buy':72, 'hold':25, 't1':11.0, 't2':8.0,  'trail':15.0, 'max_pos':20, 'max_total':35},
    'chaos':          {'buy':80, 'hold':25, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':20, 'max_total':30},
    'chaos_autumn':   {'buy':72, 'hold':20, 't1':8.0,  't2':6.0,  'trail':10.0, 'max_pos':15, 'max_total':20},
    'weak_autumn':    {'buy':70, 'hold':20, 't1':8.0,  't2':6.0,  'trail':12.0, 'max_pos':20, 'max_total':25},
    'autumn':         {'buy':68, 'hold':20, 't1':10.0, 't2':8.0,  'trail':12.0, 'max_pos':30, 'max_total':35},
    'winter':         {'buy':85, 'hold':10, 't1':5.0,  't2':4.0,  'trail':8.0,  'max_pos':5,  'max_total':10},
}

# ── V13.3c sigmoid惩罚函数（实装V13.3d版本） ──
def calc_sigmoid_penalty(price_change_pct, consecutive_drop=0, hs300_5d=0.0):
    """
    V13.3d sigmoid惩罚
    价格跌幅->惩罚分(0~50)
    系数0.25(比V13.3c的0.35更温和)
    """
    base = 1.0 / (1.0 + math.exp(-price_change_pct * 100 * 0.25))
    penalty = base * 50
    
    if consecutive_drop >= 3:
        penalty *= 1.3
    if consecutive_drop >= 5:
        penalty *= 1.2
    
    if hs300_5d < -0.03:
        penalty *= 1.15
    
    return min(50, penalty)


class BacktestEngine:
    """
    V13.3d 端到端回测引擎
    - 每天先跑season_engine v2.2判定季节
    - 再跑V13.3d评分引擎评分
    - 最后执行交易逻辑
    """
    
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        
        # 交易日列表
        self.cur.execute("""
            SELECT DISTINCT trade_date FROM daily_kline 
            WHERE trade_date >= '2024-09-02' AND trade_date <= '2026-07-17'
            ORDER BY trade_date
        """)
        self.trading_days = [str(r['trade_date']) for r in self.cur.fetchall()]
        print(f"交易日: {len(self.trading_days)}天")
        
        # 预加载K线
        print("预加载K线...")
        t0 = time.time()
        self.cur.execute("""
            SELECT ts_code, trade_date, close
            FROM daily_kline 
            WHERE trade_date >= '2020-01-01' AND close > 0
            ORDER BY ts_code, trade_date
        """)
        self.kline = defaultdict(list)
        for r in self.cur.fetchall():
            self.kline[r['ts_code']].append((str(r['trade_date']), float(r['close'])))
        print(f"  ✅ {sum(len(v) for v in self.kline.values())}条K线 ({time.time()-t0:.1f}s)")
        
        # 预加载技术指标
        print("预加载技术指标...")
        t0 = time.time()
        self.cur.execute("""
            SELECT ts_code, trade_date,
                   ma_5 as ma5, ma_20 as ma20, ma_60 as ma60, ma_120 as ma120,
                   rsi_14, macd_dif, macd_dea, macd_bar as macd_hist,
                   atr_14 as atr14, boll_upper as bb_upper, boll_lower as bb_lower, boll_width as bb_width,
                   vol_ma_5 as vol_ma5, vol_ma_20 as vol_ma20
            FROM technical_indicator
            WHERE trade_date >= '2020-01-01'
            ORDER BY ts_code, trade_date
        """)
        self.tech_indicators = defaultdict(lambda: defaultdict(dict))
        count = 0
        for r in self.cur.fetchall():
            code = r['ts_code']
            td = str(r['trade_date'])
            for k in ('ma5','ma20','ma60','ma120','rsi_14','macd_dif','macd_dea','macd_hist',
                       'atr14','bb_upper','bb_lower','bb_width','vol_ma5','vol_ma20'):
                if r.get(k) is not None:
                    self.tech_indicators[code][td][k] = float(r[k])
            count += 1
        print(f"  ✅ {count}条指标 ({time.time()-t0:.1f}s)")
        
        # 预加载监控池（评分范围）
        self.cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
        self.watch_pool = [r['ts_code'] for r in self.cur.fetchall()]
        print(f"监控池: {len(self.watch_pool)}只")
        
        # 导入季节引擎V2.2
        if 'season_engine' in sys.modules:
            del sys.modules['season_engine']
        from season_engine import SeasonEngine
        self.season_engine = SeasonEngine(use_market_breadth=False)
        
        # 缓存季节判定（避免每天重跑judge_history）
        self.season_cache = {}
        
    def get_season(self, trade_date):
        """获取某一天的季节判定"""
        if trade_date in self.season_cache:
            return self.season_cache[trade_date]
        
        td_date = datetime.strptime(trade_date, '%Y-%m-%d').date()
        result = self.season_engine.judge_market_season(target_date=td_date)
        season = result.get('market_season', 'chaos')
        regime = result.get('market_regime', 'range')
        confidence = result.get('market_confidence', 0.5)
        
        self.season_cache[trade_date] = (season, regime, confidence)
        return season, regime, confidence
    
    def compute_penalty(self, ts_code, trade_date):
        """计算V13.3d sigmoid惩罚分"""
        klines = self.kline.get(ts_code, [])
        if len(klines) < 20:
            return 0.0
        
        date_prices = [(d, p) for d, p in klines if d <= trade_date]
        if len(date_prices) < 20:
            return 0.0
        
        closes = [p for _, p in date_prices[-120:]]
        n = len(closes)
        if n < 5:
            return 0.0
        
        cp = closes[-1]
        
        # MA5/MA20
        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20 if n >= 20 else ma5
        
        # 短期涨跌幅
        r5 = (closes[-1] - closes[-6]) / closes[-6] if n >= 6 else 0
        r10 = (closes[-1] - closes[-11]) / closes[-11] if n >= 11 else 0
        r20 = (closes[-1] - closes[-21]) / closes[-21] if n >= 21 else 0
        
        # 连续下跌天数
        consec_drop = 0
        for ci in range(min(10, n-1)):
            if closes[-(ci+1)] < closes[-(ci+2)]:
                consec_drop += 1
            else:
                break
        
        penalty_score = 0.0
        
        # 1. 破MA20 → trend折价
        if ma20 > 0 and cp < ma20:
            below = (ma20 - cp) / ma20
            discount = max(0.4, 1.0 - below * 0.6)
            tloss = 55 * (1 - discount) * 0.30
            if tloss > 2:
                penalty_score += round(tloss, 1)
        
        # 2. 空头排列 (+8)
        if (ma5 > 0 and ma20 > 0 and cp < ma5 < ma20) or (ma5 > 0 and ma20 > 0 and ma5 < ma20):
            penalty_score += 8
        
        # 3. 5日/10日/20日跌幅惩罚
        if r5 < -0.05:
            p = min(25, int(abs(r5) * 180))
            penalty_score += p
        if r10 < -0.08:
            p = min(20, int(abs(r10) * 120))
            penalty_score += p
        if r20 < -0.10:
            p = min(25, int(abs(r20) * 100))
            penalty_score += p
        
        # V13.3d sigmoid替代固定惩罚
        # 选用r5/r10/r20中最严重的一个
        drop_pct = min(r5, r10, r20) if min(r5, r10, r20) < 0 else 0
        sigmoid_ps = calc_sigmoid_penalty(drop_pct, consec_drop)
        
        # 取两者中较高者
        final_penalty = max(penalty_score, sigmoid_ps)
        
        return round(final_penalty, 1)
    
    def score_stock_v133d(self, ts_code, trade_date, season):
        """
        V13.3d对单只股票的评分
        返回composite_score（0-100的原始分）
        """
        # 从technical_indicator取当日指标值
        tech = self.tech_indicators.get(ts_code, {}).get(trade_date, {})
        klines = self.kline.get(ts_code, [])
        date_prices = [(d, p) for d, p in klines if d <= trade_date]
        
        if len(date_prices) < 5:
            return 40.0  # 默认中间分
        
        closes = [p for _, p in date_prices[-120:]]
        n = len(closes)
        
        # ── 多维度评分 ──
        score = 50.0  # 基础分
        
        # 动量维度 (±20)
        ma5 = tech.get('ma5', sum(closes[-5:]) / 5 if n >= 5 else 0)
        ma20 = tech.get('ma20', sum(closes[-20:]) / 20 if n >= 20 else 0)
        cp = closes[-1]
        
        if ma5 > 0 and ma20 > 0:
            # 价格相对MA20位置
            ma20_ratio = cp / ma20
            if ma20_ratio > 1.10:
                score += 15
            elif ma20_ratio > 1.05:
                score += 10
            elif ma20_ratio > 1.02:
                score += 5
            elif ma20_ratio < 0.95:
                score -= 10
            elif ma20_ratio < 0.90:
                score -= 15
        
        # 均线结构 (±15)
        if ma5 > 0 and ma20 > 0:
            if ma5 > ma20:
                score += 8
                if tech.get('ma60', 0) and ma20 > tech['ma60']:
                    score += 5
            else:
                score -= 5
                if tech.get('ma60', 0) and ma20 < tech['ma60']:
                    score -= 8
        
        # MACD (±10)
        hist = tech.get('macd_hist', 0)
        if hist > 0:
            score += 5
            if len(closes) >= 2 and (closes[-1] > closes[-2] if len(closes) >= 2 else True):
                score += 5
        elif hist < -0.5:
            score -= 8
        
        # RSI 极端校正 (±5)
        rsi = tech.get('rsi_14', 50)
        if rsi > 75:
            score -= 5
        elif rsi > 65:
            score -= 1
        elif rsi < 25:
            score += 5
        elif rsi < 35:
            score += 1
        
        # 成交量能量 (±10)
        vol_ma5 = tech.get('vol_ma5', 0)
        vol_ma20 = tech.get('vol_ma20', 0)
        if vol_ma5 > 0 and vol_ma20 > 0:
            vol_ratio = vol_ma5 / vol_ma20
            if vol_ratio > 1.5:
                score += 8
            elif vol_ratio > 1.2:
                score += 4
            elif vol_ratio < 0.6:
                score -= 5
                if season == 'summer':
                    score -= 5  # 缩量上涨不宜追
            
            # 量价配合
            if n >= 2:
                if closes[-1] > closes[-2] and vol_ratio > 1.0:
                    score += 3
                elif closes[-1] < closes[-2] and vol_ratio > 1.5:
                    score -= 5
        
        # 布林带位置 (±5)
        bb_upper = tech.get('bb_upper', 0)
        bb_lower = tech.get('bb_lower', 0)
        if bb_upper > bb_lower > 0 and cp > 0:
            bb_pos = (cp - bb_lower) / (bb_upper - bb_lower)
            if bb_pos > 0.9:
                score -= 3
            elif bb_pos < 0.2:
                score += 3
        
        # 季节偏好修正（V13.2策略）
        if season in ('summer', 'spring'):
            score += 3  # 向上趋势加分
        elif season in ('autumn', 'winter'):
            score -= 3  # 防守期扣分
        elif season == 'chaos_spring':
            score += 1
        elif season == 'chaos_autumn':
            score -= 1
        
        # 最终限制在[0, 100]区间
        return max(0, min(100, score))
    
    def run(self, start_date='2024-09-02', end_date='2026-07-17'):
        """运行端到端回测"""
        print(f"\n{'='*60}")
        print(f"🚀 V13.3d 端到端真实回测")
        print(f"  范围: {start_date} ~ {end_date}")
        print(f"{'='*60}")
        
        # 筛选交易日
        trading_days = [d for d in self.trading_days if d >= start_date and d <= end_date]
        
        cash = INIT_CAPITAL
        positions = []  # [{'ts_code':str, 'shares':int, 'buy_price':float, 'buy_date':str, ...}]
        all_trades = []
        penalty_stats = {'hits': 0, 'total': 0.0, 'max': 0.0}
        score_stats = {'min': 100, 'max': 0, 'total': 0.0, 'count': 0}
        
        last_trade_date = {}
        
        global_t0 = time.time()
        
        for day_idx, td in enumerate(trading_days):
            if day_idx > 0 and day_idx % 50 == 0:
                pv = cash + sum(p['cost'] for p in positions)
                elapsed = time.time() - global_t0
                print(f"  📅 {td} ({day_idx}/{len(trading_days)}) | 持仓{len(positions)} | ¥{pv/10000:.0f}万 | {len(all_trades)}笔 | ⏱{elapsed:.0f}s")
            
            # 1. 季节判定
            season, regime, confidence = self.get_season(td)
            
            # 2. 季节参数
            sp = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
            buy_line = sp['buy']
            max_hold = sp['hold']
            max_pos_pct = sp['max_pos'] / 100.0
            max_total_pct = sp['max_total'] / 100.0
            t1_pct = sp['t1'] / 100.0
            t2_pct = sp['t2'] / 100.0
            trail_pct = sp['trail'] / 100.0
            
            # 3. 检查持仓 → 卖出
            new_positions = []
            for p in positions:
                # 拿当日价格
                date_prices = [(d, pr) for d, pr in self.kline.get(p['ts_code'], []) if d <= td]
                if not date_prices:
                    new_positions.append(p)
                    continue
                
                cp = date_prices[-1][1]
                hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
                profit_pct = (cp - p['buy_price']) / p['buy_price']
                p['peak_price'] = max(p.get('peak_price', p['buy_price']), cp)
                
                reason = None
                if profit_pct <= -t1_pct:
                    reason = f'止损T1({int(t1_pct*100)}%)'
                elif hold_days >= 2 and profit_pct <= -t2_pct:
                    reason = f'止损T2({int(t2_pct*100)}%)'
                elif trail_pct > 0 and p['peak_price'] > p['buy_price']:
                    dd_from_peak = (p['peak_price'] - cp) / p['peak_price']
                    if dd_from_peak >= trail_pct:
                        reason = f'止盈({int(trail_pct*100)}%)'
                elif hold_days >= max_hold:
                    reason = f'到期({hold_days}d)'
                
                if reason:
                    gross = cp * p['shares']
                    pnl = gross - p['cost'] - gross * CHARGE_RATE
                    cash += gross - gross * CHARGE_RATE
                    all_trades.append({**p, 'exit_date': td, 'exit_price': cp,
                                       'hold_days': hold_days, 'profit_pct': round(profit_pct * 100, 2),
                                       'pnl': round(pnl, 2), 'reason': reason,
                                       'season': p.get('buy_season', season)})
                else:
                    new_positions.append(p)
            
            positions = new_positions
            
            # 4. 检查持仓 → 买入
            cur_pos_val = sum(p['cost'] for p in positions)
            max_total_val = INIT_CAPITAL * max_total_pct
            
            if (cur_pos_val < max_total_val and len(positions) < MAX_POSITIONS
                and td in [d for d in self.trading_days if d <= td]):
                
                # 对监控池每只股票评分
                candidates = []
                
                # 限定每天最多检查的股票数（加速）
                check_codes = self.watch_pool[:]
                
                for code in check_codes:
                    # 已经持有就不买
                    if any(p['ts_code'] == code for p in positions):
                        continue
                    
                    # T+1限制
                    if code in last_trade_date:
                        last_td = last_trade_date[code]
                        if last_td and (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(last_td, '%Y-%m-%d')).days < 1:
                            continue
                    
                    # 评分
                    composite = self.score_stock_v133d(code, td, season)
                    if composite is None or composite < buy_line:
                        continue
                    
                    # 惩罚
                    penalty = self.compute_penalty(code, td)
                    if penalty > 0:
                        penalty_stats['hits'] += 1
                        penalty_stats['total'] += penalty
                        penalty_stats['max'] = max(penalty_stats['max'], penalty)
                    
                    usable = composite - penalty
                    if usable >= buy_line:
                        candidates.append((code, composite, usable, penalty))
                    
                    score_stats['min'] = min(score_stats['min'], composite)
                    score_stats['max'] = max(score_stats['max'], composite)
                    score_stats['total'] += composite
                    score_stats['count'] += 1
                
                # 按评分高到低买
                candidates.sort(key=lambda x: x[2], reverse=True)
                
                for code, composite, usable, penalty in candidates[:MAX_BUY_PER_DAY]:
                    if len(positions) >= MAX_POSITIONS:
                        break
                    
                    cur_pos_val = sum(p['cost'] for p in positions)
                    if cur_pos_val >= max_total_val:
                        break
                    
                    # 单只最大仓位
                    max_single = INIT_CAPITAL * max_pos_pct
                    available = min(max_single, max_total_val - cur_pos_val)
                    available = min(available, cash)
                    
                    if available < 10000:
                        continue
                    
                    # 拿当日价格
                    date_prices = [(d, pr) for d, pr in self.kline.get(code, []) if d <= td]
                    if not date_prices:
                        continue
                    cp = date_prices[-1][1]
                    
                    shares = int(available / cp / 100) * 100
                    if shares <= 0:
                        continue
                    
                    cost = shares * cp
                    cash -= cost + cost * CHARGE_RATE
                    
                    positions.append({
                        'ts_code': code,
                        'shares': shares,
                        'buy_price': cp,
                        'cost': cost,
                        'buy_date': td,
                        'peak_price': cp,
                        'buy_season': season,
                        'buy_score': composite,
                        'penalty': penalty,
                    })
                    
                    last_trade_date[code] = td
        
        # ── 清算 ──
        for p in positions:
            date_prices = [(d, pr) for d, pr in self.kline.get(p['ts_code'], []) if d <= end_date]
            if date_prices:
                cp = date_prices[-1][1]
            else:
                cp = p['buy_price']
            hold_days = (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(p['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cp - p['buy_price']) / p['buy_price'] * 100
            gross = cp * p['shares']
            pnl = gross - p['cost'] - gross * CHARGE_RATE
            cash += gross - gross * CHARGE_RATE
            all_trades.append({**p, 'exit_date': end_date, 'exit_price': cp,
                               'hold_days': hold_days, 'profit_pct': round(profit_pct, 2),
                               'pnl': round(pnl, 2), 'reason': '到期清算',
                               'season': p.get('buy_season', 'chaos')})
        
        positions = []
        final_value = cash
        
        # ── 结果统计 ──
        total_return = (final_value - INIT_CAPITAL) / INIT_CAPITAL * 100
        
        wins = [t for t in all_trades if t['profit_pct'] > 0]
        losses = [t for t in all_trades if t['profit_pct'] <= 0]
        win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
        
        avg_win = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
        
        # 最大回撤
        peak = INIT_CAPITAL
        max_dd = 0
        max_dd_date = ''
        for t in sorted(all_trades, key=lambda x: x['buy_date']):
            val = cash
            for tp in all_trades:
                if tp['buy_date'] < t.get('exit_date', '9999-99-99') and tp.get('exit_date', '0001-01-01') > t.get('exit_date', '0001-01-01'):
                    val += tp.get('pnl', 0)
            # 简化计算 - 用累计pnl轨迹
            portfolio = INIT_CAPITAL
            pnl_track = [0]
            for tr in sorted(all_trades, key=lambda x: x.get('exit_date', x['buy_date'])):
                pnl_track.append(pnl_track[-1] + tr.get('pnl', 0))
            
            for pnl in pnl_track:
                value = INIT_CAPITAL + pnl
                peak = max(peak, value)
                dd = (peak - value) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        
        # 按季节分析
        season_trades = defaultdict(list)
        for t in all_trades:
            season_trades[t['season']].append(t)
        
        # 输出
        print(f"\n{'='*60}")
        print(f"📊 V13.3d 端到端回测结果")
        print(f"{'='*60}")
        print(f"初始: ¥{INIT_CAPITAL/10000:.0f}万 → 最终: ¥{final_value/10000:.2f}万")
        print(f"总收益: {total_return:+.2f}% | 最大回撤: {max_dd:.2f}%")
        carmar = total_return / max_dd if max_dd > 0 else 0
        print(f"卡玛: {carmar:.2f}x")
        print(f"交易: {len(all_trades)}笔 | 胜率: {win_rate:.1f}% ({len(wins)}胜/{len(losses)}负)")
        print(f"均持有: {sum(t['hold_days'] for t in all_trades)/len(all_trades):.1f}d" if all_trades else "")
        print(f"均盈+{avg_win:.2f}%/均亏{avg_loss:.2f}%")
        print(f"盈亏比: {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "")
        
        if penalty_stats['hits'] > 0:
            print(f"\n📏 惩罚统计: {penalty_stats['hits']}次 | 总计{penalty_stats['total']:.0f} | 最大{penalty_stats['max']:.0f}")
        
        if score_stats['count'] > 0:
            print(f"📊 评分统计: 范围[{score_stats['min']:.0f}~{score_stats['max']:.0f}] | 均值{score_stats['total']/score_stats['count']:.1f}")
        
        print(f"\n📂 按季节分析:")
        for s in ['summer','spring','weak_spring','chaos_spring','chaos','chaos_autumn','weak_autumn','autumn','winter']:
            ts = season_trades.get(s, [])
            if ts:
                sw = sum(1 for t in ts if t['profit_pct'] > 0)
                sr = sum(t['profit_pct'] for t in ts) / len(ts)
                print(f"  {s:20s} {len(ts):3d}笔 | {sw/len(ts)*100:.0f}%胜率 | 均{sr:+.2f}% | 均{sum(t['hold_days'] for t in ts)/len(ts):.0f}d")
        
        print(f"\n🏆 TOP5:")
        for t in sorted(all_trades, key=lambda x: -x['profit_pct'])[:5]:
            print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
        
        print(f"\n💀 BOTTOM5:")
        for t in sorted(all_trades, key=lambda x: x['profit_pct'])[:5]:
            print(f"  {t['ts_code']} {t['profit_pct']:+.2f}% ({t['hold_days']}d) {t['reason']} {t['season']}")
        
        total_seconds = time.time() - global_t0
        print(f"\n⏱ {total_seconds:.0f}s")
        
        return {
            'total_return': round(total_return, 2),
            'max_drawdown': round(max_dd, 2),
            'carmar': round(carmar, 2),
            'trades': len(all_trades),
            'win_rate': round(win_rate, 1),
            'avg_profit': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
        }


if __name__ == '__main__':
    import sys as _sys
    start = _sys.argv[1] if len(_sys.argv) > 1 else '2024-09-02'
    end = _sys.argv[2] if len(_sys.argv) > 2 else '2026-07-17'
    
    conn = pymysql.connect(**DB_CFG)
    engine = BacktestEngine(conn)
    result = engine.run(start, end)
    conn.close()
