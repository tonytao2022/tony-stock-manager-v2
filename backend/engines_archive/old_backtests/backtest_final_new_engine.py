#!/usr/bin/env python3
"""
May & Main 最终优化方案 全量回测脚本
========================================
新引擎动量轨道权重：
  缠论趋势分 30% + 位置因子 15% + 结构分 10% + 动量因子 25%
  + 大单净流入 15% + 融资融券 10%
  + 换手率过滤器（量比调节）

季节参数矩阵沿用V11固化版本。

运行: python3 backtest_final_new_engine.py
输出: /tmp/backtest_final_result.json
"""
import sys, os, json, time, math
from datetime import date, timedelta, datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymysql
import tushare as ts

# ============================================================
# 数据库连接
# ============================================================
MYSQL_PWD = 'iXve1rVBXfdA4tL9'

def get_conn():
    return pymysql.connect(
        host='localhost', user='debian-sys-maint', password='iXve1rVBXfdA4tL9',
        database='stock_db_v2', charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )

# ============================================================
# 季节判定（复用season_engine逻辑）
# ============================================================
def get_season(trade_date_str):
    """获取指定日期的季节"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""SELECT season FROM season_state 
                       WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1""", (trade_date_str,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row['season']:
            return row['season']
    except:
        pass
    return 'chaos'

def get_season_params(season):
    """获取季节参数矩阵（V11固化版本）"""
    params = {
        'summer':  {'buy_line': 72, 'max_hold': 60, 't1_stop': 12, 't2_stop': 9, 
                    'p4_threshold': 55, 'p4_extend': 15, 'trailing_stop': 18, 't2_enabled': True},
        'autumn':  {'buy_line': 75, 'max_hold': 25, 't1_stop': 8, 't2_stop': 6,
                    'p4_threshold': 65, 'p4_extend': 5, 'trailing_stop': 12, 't2_enabled': True},
        'spring':  {'buy_line': 70, 'max_hold': 20, 't1_stop': 8, 't2_stop': 6,
                    'p4_threshold': 60, 'p4_extend': 5, 'trailing_stop': 12, 't2_enabled': True},
        'winter':  {'buy_line': 85, 'max_hold': 10, 't1_stop': 5, 't2_stop': 4,
                    'p4_threshold': 999, 'p4_extend': 0, 'trailing_stop': 8, 't2_enabled': False},
        'chaos':   {'buy_line': 75, 'max_hold': 25, 't1_stop': 10, 't2_stop': 8,
                    'p4_threshold': 65, 'p4_extend': 5, 'trailing_stop': 12, 't2_enabled': False},
    }
    return params.get(season, params['chaos'])

# ============================================================
# 因子计算
# ============================================================

def calc_position_score(ts_code, trade_date_str):
    """位置因子：250日均线偏离度 → 0~100分
    公式：(当前价 - 250日均线) / 250日均线
    偏离度 -30%以下=0分, +30%以上=100分
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        # 获取当前收盘价
        cur.execute("""SELECT close FROM daily_kline_qfq 
                       WHERE ts_code=%s AND trade_date=%s""", (ts_code, trade_date_str))
        row = cur.fetchone()
        if not row or not row['close']:
            cur.close(); conn.close()
            return 50, 0
        
        close = float(row['close'])
        
        # 获取250日均线
        cur.execute("""SELECT AVG(close) as ma250 FROM daily_kline_qfq 
                       WHERE ts_code=%s AND trade_date <= %s 
                       AND trade_date >= DATE_SUB(%s, INTERVAL 250 DAY)""", 
                    (ts_code, trade_date_str, trade_date_str))
        row = cur.fetchone()
        cur.close(); conn.close()
        
        if not row or not row['ma250'] or float(row['ma250']) == 0:
            return 50, 0
        
        ma250 = float(row['ma250'])
        deviation = (close - ma250) / ma250  # 偏离度，如0.05表示偏离5%
        
        # 映射到0~100分：-30%以下=0, +30%以上=100
        score = (deviation + 0.30) / 0.60 * 100
        score = max(0, min(100, score))
        
        return round(score, 2), round(deviation, 4)
    except:
        return 50, 0


def calc_momentum_score(ts_code, trade_date_str):
    """动量因子：过去20日涨幅 → 0~100分
    负收益=0分, +50%以上=100分
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        cur.execute("""SELECT close FROM daily_kline_qfq 
                       WHERE ts_code=%s AND trade_date <= %s 
                       ORDER BY trade_date DESC LIMIT 1""", (ts_code, trade_date_str))
        row = cur.fetchone()
        if not row or not row['close']:
            cur.close(); conn.close()
            return 50
        
        close_now = float(row['close'])
        
        # 20个交易日前
        cur.execute("""SELECT close FROM daily_kline_qfq 
                       WHERE ts_code=%s AND trade_date <= %s 
                       ORDER BY trade_date DESC LIMIT 20,1""", (ts_code, trade_date_str))
        row = cur.fetchone()
        cur.close(); conn.close()
        
        if not row or not row['close'] or float(row['close']) == 0:
            return 50
        
        close_before = float(row['close'])
        change_pct = (close_now - close_before) / close_before
        
        # 映射：0%以下=0分, 50%以上=100分
        score = change_pct / 0.50 * 100
        score = max(0, min(100, score))
        
        return round(score, 2)
    except:
        return 50


def calc_moneyflow_score(ts_code, trade_date_str):
    """大单净流入因子：近5日大单+特大单净流入 → 0~100分
    使用 moneyflow 表
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        cur.execute("""SELECT 
            COALESCE(SUM(buy_lg_amount + buy_elg_amount - sell_lg_amount - sell_elg_amount), 0) as net_flow
                       FROM moneyflow 
                       WHERE ts_code=%s AND trade_date <= %s 
                       AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)""",
                    (ts_code, trade_date_str, trade_date_str))
        row = cur.fetchone()
        cur.close(); conn.close()
        
        if not row:
            return 50
        
        net_flow = float(row['net_flow'])  # 万元
        
        # 净流入500万以下=0, 净流入5亿以上=100
        score = (net_flow + 500) / (50000 + 500) * 100
        score = max(0, min(100, score))
        
        return round(score, 2)
    except:
        return 50


def calc_margin_score(ts_code, trade_date_str):
    """融资融券因子：近5日融资买入额变化 → 0~100分
    使用 margin_detail 表
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        # 近5日融资买入额均值
        cur.execute("""SELECT AVG(rzmre) as avg_rz FROM margin_detail 
                       WHERE ts_code=%s AND trade_date <= %s 
                       AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
                       AND rzmre IS NOT NULL""",
                    (ts_code, trade_date_str, trade_date_str))
        row = cur.fetchone()
        cur.close(); conn.close()
        
        if not row or not row['avg_rz']:
            return 50
        
        avg_rz = float(row['avg_rz'])  # 元
        
        # 融资买入额100万以下=0, 5亿以上=100
        score = math.log10(max(1, avg_rz / 10000)) / 5 * 100  # 对数映射
        score = max(0, min(100, score))
        
        return round(score, 2)
    except:
        return 50


def calc_vol_ratio(ts_code, trade_date_str):
    """量比：今日vol / 前20日均vol
    用于换手率过滤器
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        cur.execute("""SELECT k.vol / NULLIF(ma.avg_vol, 0) as vr
                       FROM daily_kline_qfq k
                       JOIN (SELECT AVG(vol) as avg_vol FROM daily_kline_qfq 
                             WHERE ts_code=%s AND trade_date < %s 
                             AND trade_date >= DATE_SUB(%s, INTERVAL 20 DAY)) ma ON 1=1
                       WHERE k.ts_code=%s AND k.trade_date=%s""",
                    (ts_code, trade_date_str, trade_date_str, ts_code, trade_date_str))
        row = cur.fetchone()
        cur.close(); conn.close()
        
        if row and row['vr'] is not None:
            return float(row['vr'])
    except:
        pass
    return 1.0


def get_trend_score(ts_code, trade_date_str):
    """缠论趋势分：从 trend_score 表获取 composite_score"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""SELECT composite_score FROM trend_score 
                       WHERE ts_code=%s AND trade_date=%s""", (ts_code, trade_date_str))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row['composite_score'] is not None:
            return float(row['composite_score'])
    except:
        pass
    return 50


def get_structure_score(ts_code, trade_date_str):
    """缠论结构分：从 chanlun_structure 表获取 structure_score"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        # chanlun_structure 表可能滞后，允许3天内最近的有效记录
        cur.execute("""SELECT structure_score FROM chanlun_structure 
                       WHERE ts_code=%s AND trade_date <= %s
                       AND structure_score IS NOT NULL
                       ORDER BY trade_date DESC LIMIT 1""", (ts_code, trade_date_str))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row['structure_score'] is not None:
            return float(row['structure_score']) * 2  # 0~50→0~100
    except:
        pass
    return 50


# ============================================================
# 新引擎综合评分
# ============================================================
def score_stock_new(ts_code, trade_date_str):
    """新引擎动量轨道评分
    趋势30% + 位置15% + 结构10% + 动量25% + 大单15% + 融资融券10%
    + 换手率调节（量比0.3~2.0范围内不调节，超出范围扣/加分）
    """
    trend = get_trend_score(ts_code, trade_date_str)
    pos_score, pos_dev = calc_position_score(ts_code, trade_date_str)
    struct = get_structure_score(ts_code, trade_date_str)
    momentum = calc_momentum_score(ts_code, trade_date_str)
    mf = calc_moneyflow_score(ts_code, trade_date_str)
    margin = calc_margin_score(ts_code, trade_date_str)
    vr = calc_vol_ratio(ts_code, trade_date_str)
    
    # 综合评分
    composite = (trend * 0.30 + pos_score * 0.15 + struct * 0.10 + 
                 momentum * 0.25 + mf * 0.15 + margin * 0.10)
    
    # 换手率过滤器
    if vr < 0.3:
        composite *= 0.95  # 量比太低，流动性不足
    elif vr > 5.0:
        composite *= 0.90  # 量比异常高，过热/出货风险
    elif 0.8 <= vr <= 2.0:
        composite *= 1.02  # 量比正常偏活跃，加分
    
    composite = max(0, min(100, composite))
    
    return round(composite, 2), {
        'trend': trend,
        'position': pos_score,
        'position_dev': pos_dev,
        'structure': struct,
        'momentum': momentum,
        'moneyflow': mf,
        'margin': margin,
        'vol_ratio': round(vr, 2),
    }


# ============================================================
# 回测核心逻辑
# ============================================================
def backtest():
    pro = ts.pro_api()
    conn = get_conn()
    cur = conn.cursor()
    
    # 获取回测池股票
    cur.execute("SELECT ts_code, name FROM backtest_pool")
    pool = {r['ts_code']: r.get('name', '') for r in cur.fetchall()}
    pool_codes = list(pool.keys())
    print(f"回测池: {len(pool_codes)}只")
    
    # 获取所有交易日
    cur.execute("""SELECT DISTINCT trade_date FROM daily_kline_qfq 
                   WHERE trade_date BETWEEN '2023-01-03' AND '2026-06-12' 
                   ORDER BY trade_date""")
    trade_dates = [r['trade_date'].strftime('%Y-%m-%d') for r in cur.fetchall()]
    print(f"交易日: {len(trade_dates)}个 ({trade_dates[0]} ~ {trade_dates[-1]})")
    
    # ============================================================
    # 模拟交易
    # ============================================================
    INITIAL_CAPITAL = 1_000_000  # 100万
    capital = INITIAL_CAPITAL
    positions = {}  # ts_code -> {'buy_date','buy_price','qty','season','high_since_buy'}
    trades = []
    
    t_start = time.time()
    
    for i, td in enumerate(trade_dates):
        if (i+1) % 100 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i+1}/{len(trade_dates)}] {td} | 持仓{len(positions)}只 | 市值{capital:.0f} | {elapsed:.0f}s")
        
        td_str = td.replace('-', '')
        season = get_season(td)
        sp = get_season_params(season)
        
        # --- 卖出检查 ---
        to_sell = []
        for code, pos in positions.items():
            # 获取当前价
            cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
            row = cur.fetchone()
            if not row or not row['close']:
                continue
            cur_price = float(row['close'])
            hold_days = (datetime.strptime(td, '%Y-%m-%d') - datetime.strptime(pos['buy_date'], '%Y-%m-%d')).days
            profit_pct = (cur_price - pos['buy_price']) / pos['buy_price'] * 100
            
            # 更新最高价
            if cur_price > pos.get('high_since_buy', pos['buy_price']):
                pos['high_since_buy'] = cur_price
            
            # 移动止盈：从最高点回撤超过trailing_stop%
            high = pos.get('high_since_buy', pos['buy_price'])
            trailing_drawdown = (high - cur_price) / high * 100
            
            # 检查卖出条件
            sell_reason = None
            
            # T1止损：从买入价跌幅超过t1_stop%
            if profit_pct <= -sp['t1_stop']:
                sell_reason = f'T1_止损{t1_stop}%'
            
            # T2止损：从最高点回撤超过t2_stop%（如果有盈利时）
            elif profit_pct > 0 and trailing_drawdown >= sp['t2_stop'] and sp['t2_enabled']:
                sell_reason = f'T2_回撤{t2_stop}%'
            
            # 移动止盈
            elif profit_pct > 0 and trailing_drawdown >= sp['trailing_stop']:
                sell_reason = f'移动止盈{trailing_stop}%'
            
            # P4延期检查：到了max_hold
            elif hold_days >= sp['max_hold']:
                # 如果评分>=p4_threshold，延期
                new_score, _ = score_stock_new(code, td)
                if new_score >= sp['p4_threshold'] and sp['p4_extend'] > 0:
                    pos['max_hold'] = hold_days + sp['p4_extend']
                else:
                    sell_reason = f'持有到期{max_hold}日'
            
            if sell_reason:
                to_sell.append((code, cur_price, profit_pct, sell_reason, hold_days, season))
        
        for code, price, pct, reason, hold_days, sea in to_sell:
            pos = positions.pop(code)
            revenue = price * pos['qty']
            capital += revenue
            trades.append({
                'ts_code': code,
                'name': pool.get(code, ''),
                'buy_date': pos['buy_date'],
                'sell_date': td,
                'hold_days': hold_days,
                'buy_price': pos['buy_price'],
                'sell_price': price,
                'profit_pct': round(pct, 2),
                'season': sea,
                'exit_reason': reason,
                'qty': pos['qty'],
            })
        
        # --- 买入检查 ---
        if len(positions) < 8:  # 最多持仓8只
            # 对回测池所有不在持仓的股票评分
            candidates = []
            scores = score_all_stocks(pool_codes, td, positions, cur)
            for code, score, details in scores:
                if score >= sp['buy_line']:
                    candidates.append((code, score, details))
            
            # 按评分排序取Top N
            candidates.sort(key=lambda x: x[1], reverse=True)
            max_buy = 8 - len(positions)
            for code, score, details in candidates[:max_buy]:
                # 获取价格
                cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, td))
                row = cur.fetchone()
                if not row or not row['close']:
                    continue
                price = float(row['close'])
                
                # 买入：等分资金
                qty = int((capital * 0.12) / price / 100) * 100  # 每只约12%仓位
                if qty < 100:
                    continue
                
                cost = qty * price
                if cost > capital:
                    continue
                
                capital -= cost
                positions[code] = {
                    'buy_date': td,
                    'buy_price': price,
                    'qty': qty,
                    'season': season,
                    'high_since_buy': price,
                    'max_hold': sp['max_hold'],
                }
    
    elapsed = time.time() - t_start
    cur.close()
    conn.close()
    
    # ============================================================
    # 汇总结果
    # ============================================================
    final_value = capital + sum(p['qty'] * get_last_price(p['buy_date'], p['buy_price']) for p in positions.values())
    
    print(f"\n{'='*60}")
    print(f"回测完成！耗时: {elapsed:.0f}s")
    print(f"{'='*60}")
    print(f"初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"最终市值: {final_value:,.0f}")
    total_return = (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    print(f"总收益率: {total_return:+.2f}%")
    print(f"交易笔数: {len(trades)}")
    
    if trades:
        wins = [t for t in trades if t['profit_pct'] > 0]
        losses = [t for t in trades if t['profit_pct'] <= 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
        profit_factor = abs(sum(t['profit_pct'] for t in wins) / sum(t['profit_pct'] for t in losses)) if losses and sum(t['profit_pct'] for t in losses) != 0 else float('inf')
        avg_hold = sum(t['hold_days'] for t in trades) / len(trades)
        
        print(f"胜率: {win_rate:.2f}%")
        print(f"平均盈利: {avg_win:.2f}%")
        print(f"平均亏损: {avg_loss:.2f}%")
        print(f"盈利因子: {profit_factor:.2f}")
        print(f"平均持有: {avg_hold:.1f}天")
        
        # 按持有天数分组
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
        
        print(f"\n持有期分组:")
        for k, v in sorted(by_hold.items()):
            w = [t for t in v if t['profit_pct'] > 0]
            wr = len(w)/len(v)*100 if v else 0
            avg = sum(t['profit_pct'] for t in v)/len(v) if v else 0
            print(f"  {k}: {len(v)}笔, 胜率{wr:.1f}%, 均收益{avg:+.2f}%")
        
        # 按季节分组
        by_season = defaultdict(list)
        for t in trades:
            by_season[t['season']].append(t)
        
        print(f"\n季节分组:")
        for k, v in sorted(by_season.items()):
            w = [t for t in v if t['profit_pct'] > 0]
            wr = len(w)/len(v)*100 if v else 0
            avg = sum(t['profit_pct'] for t in v)/len(v) if v else 0
            print(f"  {k}: {len(v)}笔, 胜率{wr:.1f}%, 均收益{avg:+.2f}%")
    
    # 保存结果
    result = {
        'initial_capital': INITIAL_CAPITAL,
        'final_value': round(final_value, 2),
        'total_return_pct': round(total_return, 2),
        'trade_count': len(trades),
        'win_rate': round(win_rate, 2) if trades else 0,
        'profit_factor': round(profit_factor, 2) if trades else 0,
        'avg_hold_days': round(avg_hold, 1) if trades else 0,
        'elapsed_seconds': round(elapsed, 0),
        'trades': trades,
    }
    
    with open('/tmp/backtest_final_result.json', 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到 /tmp/backtest_final_result.json")


def score_all_stocks(pool_codes, td, positions, cur):
    """批量评分所有股票"""
    results = []
    for code in pool_codes:
        if code in positions:
            continue
        score, details = score_stock_new(code, td.replace('-', ''))
        if score > 0:
            results.append((code, score, details))
    return results


def get_last_price(trade_date, fallback_price):
    """获取最后价格（用于未平仓持仓）"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""SELECT close FROM daily_kline_qfq 
                       WHERE trade_date = (SELECT MAX(trade_date) FROM daily_kline_qfq) 
                       LIMIT 1""")
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row['close']:
            return float(row['close'])
    except:
        pass
    return fallback_price


if __name__ == '__main__':
    # 先测试单只股票评分
    print("=== 测试新引擎评分 ===")
    for code in ['002475.SZ', '300124.SZ', '601012.SH']:
        score, details = score_stock_new(code, '20260610')
        print(f"  {code}: {score}")
        print(f"    趋势{details['trend']:.0f} 位置{details['position']:.0f}(偏离{details['position_dev']:.2%}) 结构{details['structure']:.0f}")
        print(f"    动量{details['momentum']:.0f} 大单{details['moneyflow']:.0f} 融资{details['margin']:.0f}")
        print(f"    量比{details['vol_ratio']:.2f}")
    
    print("\n=== 启动全量回测 ===")
    backtest()
