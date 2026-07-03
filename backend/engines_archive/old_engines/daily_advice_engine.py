#!/usr/bin/env python3
"""
操作建议引擎 — 根据季节+评分+持仓生成每日操作建议
"""
import sys, os, json, math
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from step_strategy_engine import get_conn
# # from season_engine import SeasonEngine

DAYS_CN = ['周一','周二','周三','周四','周五','周六','周日']
SEASON_EMOJI = {
    'summer': '☀️', 'chaos_spring': '🌤️', 'chaos': '🌪️',
    'chaos_autumn': '🌥️', 'autumn': '🍂', 'winter': '❄️', 'spring': '🌸'
}

def get_today_config():
    """获取今天的季节和对应策略"""
    conn = get_conn(); cur = conn.cursor()
    
    # 1. 今天是什么季节
    cur.execute("""
        SELECT season, hengjiyuan_level, raw_score, confidence, regime, scoring_strategy
        FROM season_state WHERE index_code='MARKET'
        ORDER BY trade_date DESC LIMIT 1
    """)
    r = cur.fetchone()
    if not r:
        return {'season': 'chaos', 'season_label': '🌪️ 混沌(观望)', 'params': {}}
    
    season = r[0]; hengji = r[1] or 'weak_heng'; raw = float(r[2] or 0); conf = float(r[3] or 0)
    regime = r[4] or 'range'; scoring = r[5] or 'momentum'
    emoji = SEASON_EMOJI.get(season, '🌪️')
    
    # 季节中文标
    season_labels = {
        'summer': f'{emoji} 夏季(持有)',
        'chaos_spring': f'{emoji} 弱春(偏多)',
        'spring': f'{emoji} 春季(进攻)',
        'chaos': f'{emoji} 混沌(观望)',
        'chaos_autumn': f'{emoji} 弱秋(偏空)',
        'autumn': f'{emoji} 秋季(防守)',
        'winter': f'{emoji} 冬季(休眠)',
    }
    
    # 2. 读取对应季节的策略参数
    cur.execute("""
        SELECT buy_min_score, max_pos_pct, stop_loss_pct, trailing_stop_pct,
               cool_days, max_hold_days, p1_score, p2_score, p3_score
        FROM strategy_config WHERE season_type=%s AND is_active=1
        LIMIT 1
    """, (season,))
    sp = cur.fetchone()
    
    if not sp:
        # fallback到ALL/基线
        cur.execute("SELECT buy_min_score, max_pos_pct, stop_loss_pct, trailing_stop_pct, cool_days, max_hold_days, p1_score, p2_score, p3_score FROM strategy_config WHERE season_type='ALL' AND is_active=1 LIMIT 1")
        sp = cur.fetchone()
    
    params = {
        'threshold': int(sp[0]), 'max_pos_pct': int(sp[1]), 'stop_loss': float(sp[2]),
        'trailing_stop': float(sp[3]), 'cool_days': int(sp[4]), 'max_hold': int(sp[5]),
        'p1': int(sp[6]), 'p2': int(sp[7]), 'p3': int(sp[8]),
    }
    
    cur.close(); conn.close()
    
    return {
        'season': season,
        'season_label': season_labels.get(season, f'{emoji} {season}'),
        'hengji': hengji,
        'raw_score': raw,
        'confidence': conf,
        'regime': regime,
        'scoring_strategy': scoring,
        'params': params,
    }

def get_top_stocks(trade_date=None, top_n=5):
    """获取今日Top N评分股票"""
    conn = get_conn(); cur = conn.cursor()
    
    if trade_date is None:
        cur.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
        trade_date = str(cur.fetchone()[0])
    
    cur.execute("""
        SELECT ss.ts_code, sb.name, ss.composite_score, ss.composite_score,
               ss.direction, ss.buy_sell_point, ss.position_pct,
               ss.reason_chain
        FROM strategy_signal ss
        LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
        WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0
        ORDER BY ss.composite_score DESC, ss.composite_score DESC
        LIMIT %s
    """, (trade_date, top_n))
    
    stocks = []
    for r in cur.fetchall():
        calib = float(r[2] or 0); comp = float(r[3] or 0)
        stocks.append({
            'ts_code': r[0], 'name': r[1] or '',
            'score': max(calib, comp),
            'direction': r[4] or '',
            'buy_sell_point': r[5] or '',
            'position_pct': float(r[6] or 0),
        })
    
    cur.close(); conn.close()
    return stocks

def get_holding_advice(params):
    """获取当前持仓的操作建议"""
    conn = get_conn(); cur = conn.cursor()
    
    cur.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
    trade_date = str(cur.fetchone()[0])
    
    # 读取当前持仓
    cur.execute("""
        SELECT ts_code, name, shares, cost_price, current_price, profit_pct, buy_date, status
        FROM portfolio_holdings WHERE status='HOLDING'
    """)
    
    holdings = []
    for r in cur.fetchall():
        code = r[0]; name = r[1] or ''; shares = int(r[2]); cost = float(r[3] or 0)
        cur_price = float(r[4] or 0); profit = float(r[5] or 0); buy_date = str(r[6])
        
        # 今天的评分
        cur.execute("""
            SELECT composite_score, composite_score FROM strategy_signal
            WHERE ts_code=%s AND trade_date=%s LIMIT 1
        """, (code, trade_date))
        sr = cur.fetchone()
        score = float(sr[0] or sr[1] or 0) if sr else 0
        
        # 持有天数
        hold_days = (date.fromisoformat(trade_date) - date.fromisoformat(buy_date)).days if buy_date else 0
        
        # 建议
        advice, reason = generate_advice(code, score, profit, hold_days, params)
        
        holdings.append({
            'ts_code': code, 'name': name, 'shares': shares, 'cost': cost,
            'current_price': cur_price, 'profit_pct': round(profit, 2),
            'buy_date': buy_date, 'hold_days': hold_days,
            'current_score': round(score, 1), 'advice': advice, 'reason': reason,
            'market_value': round(cur_price * shares, 2),
        })
    
    cur.close(); conn.close()
    return holdings

def generate_advice(code, score, profit, hold_days, params):
    """针对单只股票生成操作建议"""
    th = params['threshold']; p1 = params['p1']; sl = params['stop_loss']
    ts = params['trailing_stop']; max_hold = params['max_hold']
    
    # 止损检查
    if profit <= -sl:
        return '🛑 卖出', f'亏损{profit:.1f}%触发止损线-{sl:.0f}%，无条件平仓'
    
    # 移动止盈（简化：如果有浮盈且持有超过10天）
    if profit >= ts and hold_days >= 10:
        return '💰 考虑止盈', f'盈利{profit:.1f}%超过止盈线{ts:.0f}%，建议分批止盈'
    
    # 持有到期
    if hold_days >= max_hold:
        return '⏰ 到期卖出', f'持有{hold_days}日达上限{max_hold}日，建议卖出'
    
    # 检视点检查
    if hold_days >= 5 and hold_days <= 6 and score < p1:
        return '🔴 卖出', f'5日检视评分{score}<{p1}，不达标建议卖出'
    
    if hold_days >= 10 and hold_days <= 11 and score < params['p2']:
        return '🔴 卖出', f'10日检视评分{score}<{params["p2"]}，不达标建议卖出'
    
    if hold_days >= 20 and hold_days <= 21 and score < params['p3']:
        return '🔴 卖出', f'20日检视评分{score}<{params["p3"]}，不达标建议卖出'
    
    # 持有
    if profit > 0:
        return '🟢 持有', f'评分{score}已持有{hold_days}日，盈利{profit:.1f}%，趋势良好继续持有'
    else:
        return '🟡 观察', f'评分{score}已持有{hold_days}日，当前亏损{profit:.1f}%，观察等待反弹'

def get_buy_candidates(params, top_n=5):
    """获取今日Top买入候选"""
    conn = get_conn(); cur = conn.cursor()
    
    cur.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
    trade_date = str(cur.fetchone()[0])
    
    th = params['threshold']
    
    # 获取当前持仓代码（排除已有）
    cur.execute("SELECT ts_code FROM portfolio_holdings WHERE status='HOLDING'")
    holding_codes = set(r[0] for r in cur.fetchall())
    
    # 获取今日评分达标的股票（排除持仓中、排除冷却期）
    cur.execute("""
        SELECT ss.ts_code, sb.name, ss.composite_score, ss.composite_score,
               ss.buy_sell_point, ss.direction, ss.reason_chain
        FROM strategy_signal ss
        LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
        WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0
        AND ss.composite_score >= %s
        ORDER BY ss.composite_score DESC
        LIMIT %s
    """, (trade_date, th, top_n + len(holding_codes)))
    
    candidates = []
    for r in cur.fetchall():
        code = r[0]
        if code in holding_codes: continue
        calib = float(r[2] or 0); comp = float(r[3] or 0)
        candidates.append({
            'ts_code': code, 'name': r[1] or '',
            'score': max(calib, comp),
            'buy_point': r[4] or '',
            'direction': r[5] or '',
        })
        if len(candidates) >= top_n:
            break
    
    cur.close(); conn.close()
    return candidates

def generate_report():
    """生成完整的操作建议报告"""
    config = get_today_config()
    params = config['params']
    
    today = date.today()
    weekday = DAYS_CN[today.weekday()]
    
    report = []
    report.append(f"📋 今日操作建议 — {today}（{weekday}）")
    report.append("")
    
    # 市场状态
    report.append(f"【市场状态】")
    report.append(f"  季节：{config['season_label']} | 置信度：{config['confidence']:.0%}")
    report.append(f"  恒纪元：{config['hengji']} | 体制：{config['regime']}")
    report.append(f"  策略：{config['scoring_strategy']} | 指数评分：{config['raw_score']:+.1f}")
    report.append("")
    
    # 今日参数
    report.append(f"【今日策略参数】")
    report.append(f"  买入阈值：≥{params['threshold']}分 | 单只仓位上限：{params['max_pos_pct']}%")
    report.append(f"  止损：-{params['stop_loss']:.0f}% | 移动止盈：{params['trailing_stop']:.0f}%回撤")
    report.append(f"  检视点：5日({params['p1']}) / 10日({params['p2']}) / 20日({params['p3']}) | 冷却期：{params['cool_days']}日")
    report.append(f"  总仓位上限：90% | 最大持有：{params['max_hold']}日")
    report.append("")
    
    # 持仓操作
    holdings = get_holding_advice(params)
    total_mv = sum(h['market_value'] for h in holdings)
    total_cost = sum(h['cost'] * h['shares'] for h in holdings)
    total_profit = round((total_mv - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0
    
    report.append(f"【持仓操作建议】({len(holdings)}只 / 市值¥{total_mv:,.0f} / 总盈亏{total_profit:+.1f}%)")
    if not holdings:
        report.append("  （当前无持仓）")
    else:
        for h in holdings:
            report.append(f"  {h['advice']} {h['ts_code']} {h['name']}")
            report.append(f"     持仓{h['shares']}股 | 成本¥{h['cost']:.2f} | 现价¥{h['current_price']:.2f} | 盈亏{h['profit_pct']:+.1f}%")
            report.append(f"     持有{h['hold_days']}日 | 评分{h['current_score']} | {h['reason']}")
    report.append("")
    
    # 买入候选
    candidates = get_buy_candidates(params)
    report.append(f"【今日买入候选】Top {len(candidates)}（阈值≥{params['threshold']}分）")
    if not candidates:
        report.append("  （今日无达标的买入候选）")
    else:
        for i, c in enumerate(candidates, 1):
            report.append(f"  {i}. {c['ts_code']} {c['name']} | 评分{c['score']} {c['direction']}")
    
    report.append("")
    report.append("---")
    report.append(f"⚠️ 以上建议基于P6评分+季节动态策略V6自动生成，仅供参考，不构成投资建议。")
    
    return '\n'.join(report)

if __name__ == '__main__':
    config = get_today_config()
    params = config['params']
    today_str = str(date.today())
    
    report = generate_report()
    print(report)
    
    # 保存文本报告
    with open('/tmp/daily_advice_report.txt', 'w') as f:
        f.write(report)
    
    # 保存JSON（供前端advice.html读取）
    import json
    holdings = get_holding_advice(params)
    candidates = get_buy_candidates(params)
    json_data = {
        'status': 'ok',
        'date': today_str,
        'season': config['season'],
        'season_label': config['season_label'],
        'hengji': config['hengji'],
        'raw_score': config['raw_score'],
        'confidence': config['confidence'],
        'regime': config['regime'],
        'scoring_strategy': config['scoring_strategy'],
        'params': params,
        'holdings': [dict(h) for h in holdings],
        'candidates': [dict(c) for c in candidates],
    }
    json_path = "/var/www/html/stock-v2/advice_data.json"
    with open(json_path, 'w') as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    print(f"✅ 报告已保存: /tmp/daily_advice_report.txt")
    print(f"✅ JSON已保存: {json_path}")
