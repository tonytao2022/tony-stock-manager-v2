#!/usr/bin/env python3
"""
交易记录管理 — 记录每笔买入/卖出，加权平均持仓日期
"""
import os, sys, json, math
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from step_strategy_engine import get_conn, PWD

def init_trade_table():
    """初始化交易记录表"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_records (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(16) NOT NULL COMMENT '股票代码',
            name VARCHAR(50) DEFAULT '' COMMENT '股票名称',
            trade_date DATE NOT NULL COMMENT '交易日期',
            direction VARCHAR(4) NOT NULL COMMENT 'BUY/SELL',
            qty INT NOT NULL COMMENT '股数',
            price DECIMAL(12,3) NOT NULL COMMENT '成交价',
            amount DECIMAL(16,2) NOT NULL COMMENT '成交金额',
            commission DECIMAL(10,2) DEFAULT 0 COMMENT '佣金',
            notes VARCHAR(200) DEFAULT '' COMMENT '备注',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_ts_code (ts_code),
            INDEX idx_trade_date (trade_date),
            INDEX idx_direction (direction)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易记录表'
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("✅ trade_records 表初始化完成")


def add_trade(ts_code, name, trade_date, direction, qty, price, commission=0, notes=''):
    """添加一笔交易记录"""
    conn = get_conn()
    cur = conn.cursor()
    amount = round(qty * price, 2)
    cur.execute("""
        INSERT INTO trade_records (ts_code, name, trade_date, direction, qty, price, amount, commission, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (ts_code, name, trade_date, direction, qty, price, amount, commission, notes))
    conn.commit()
    trade_id = cur.lastrowid
    cur.close()
    conn.close()
    return trade_id


def calc_weighted_avg_hold_days(ts_code, current_date=None):
    """计算某只股票的加权平均持仓天数
    规则：每笔买入的股数×持有天数 / 总持仓股数
    不加权卖出，卖出直接减少股数
    """
    conn = get_conn()
    cur = conn.cursor()
    
    if current_date is None:
        current_date = date.today()
    
    # 获取所有买入记录（按时间正序）
    cur.execute("""
        SELECT trade_date, qty, price, amount FROM trade_records
        WHERE ts_code=%s AND direction='BUY'
        ORDER BY trade_date ASC
    """, (ts_code,))
    buys = cur.fetchall()
    
    # 获取所有卖出记录
    cur.execute("""
        SELECT trade_date, qty FROM trade_records
        WHERE ts_code=%s AND direction='SELL'
        ORDER BY trade_date ASC
    """, (ts_code,))
    sells = cur.fetchall()
    
    # 模拟持仓：FIFO匹配，计算股数+加权成本
    total_qty = 0
    total_cost = 0.0
    weighted_days = 0.0
    lots = []  # (trade_date, qty, price)
    
    # 先填充买入
    for b in buys:
        b_date = b[0] if isinstance(b, dict) else b[0]
        b_qty = int(b[1] if isinstance(b, dict) else b[1])
        b_price = float(b[2] if isinstance(b, dict) else b[2])
        lots.append({'date': b_date, 'qty': b_qty, 'price': b_price})
    
    # 卖出匹配（FIFO）
    for s in sells:
        s_date = s[0] if isinstance(s, dict) else s[0]
        s_qty = int(s[1] if isinstance(s, dict) else s[1])
        remaining = s_qty
        new_lots = []
        for lot in lots:
            if remaining <= 0:
                new_lots.append(lot)
                continue
            if lot['qty'] <= remaining:
                remaining -= lot['qty']
                # 这个lot全卖了
            else:
                # 这个lot卖一部分
                lot['qty'] -= remaining
                remaining = 0
                new_lots.append(lot)
        lots = new_lots
    
    # 计算加权平均
    for lot in lots:
        total_qty += lot['qty']
        hold_days = (current_date - lot['date']).days
        weighted_days += lot['qty'] * hold_days
        total_cost += lot['qty'] * lot['price']
    
    avg_hold_days = round(weighted_days / total_qty, 1) if total_qty > 0 else 0
    avg_cost = round(total_cost / total_qty, 3) if total_qty > 0 else 0
    
    cur.close()
    conn.close()
    
    return {
        'ts_code': ts_code,
        'current_qty': total_qty,
        'avg_cost': avg_cost,
        'avg_hold_days': avg_hold_days,
        'lots': len(lots),
    }


def sync_to_portfolio(ts_code, current_price=None):
    """将trade_records的持仓同步到portfolio_holdings"""
    conn = get_conn()
    cur = conn.cursor()
    
    # 获取加权数据
    data = calc_weighted_avg_hold_days(ts_code)
    
    if data['current_qty'] <= 0:
        # 如果无持仓，将portfolio_holdings标记为SOLD
        cur.execute("""
            UPDATE portfolio_holdings SET status='SOLD', qty=0, avail_qty=0
            WHERE ts_code=%s AND status='HOLDING'
        """, (ts_code,))
        conn.commit()
        cur.close(); conn.close()
        return {'status': 'SOLD', 'qty': 0}
    
    # 获取当前价格（如果没传则从数据库取最新的）
    if current_price is None:
        cur.execute("""
            SELECT close FROM daily_kline_qfq 
            WHERE ts_code=%s AND trade_date <= CURDATE()
            ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        r = cur.fetchone()
        current_price = float(r[0]) if r else data['avg_cost']
    
    # 获取股票名称
    name = ''
    cur.execute("SELECT name FROM stock_basic WHERE ts_code=%s LIMIT 1", (ts_code,))
    r = cur.fetchone()
    if r: name = r[0] if isinstance(r, dict) else r[0]
    
    market_value = round(current_price * data['current_qty'], 2)
    cost_total = round(data['avg_cost'] * data['current_qty'], 2)
    profit_amount = round(market_value - cost_total, 2)
    profit_pct = round((current_price - data['avg_cost']) / data['avg_cost'] * 100, 3) if data['avg_cost'] > 0 else 0
    
    # buy_date取最早的未卖出买入日期
    cur.execute("""
        SELECT MIN(trade_date) as earliest FROM trade_records
        WHERE ts_code=%s AND direction='BUY'
    """, (ts_code,))
    r = cur.fetchone()
    earliest_buy = r[0] if r else date.today()
    
    # upsert到portfolio_holdings
    cur.execute("""
        SELECT id FROM portfolio_holdings WHERE ts_code=%s AND status='HOLDING' LIMIT 1
    """, (ts_code,))
    existing = cur.fetchone()
    
    if existing:
        cur.execute("""
            UPDATE portfolio_holdings SET
                qty=%s, avail_qty=%s, current_price=%s, cost_price=%s,
                market_value=%s, profit_amount=%s, profit_pct=%s,
                buy_date=%s, updated_at=NOW()
            WHERE ts_code=%s AND status='HOLDING'
        """, (data['current_qty'], data['current_qty'], current_price, data['avg_cost'],
              market_value, profit_amount, profit_pct, earliest_buy, ts_code))
    else:
        cur.execute("""
            INSERT INTO portfolio_holdings 
            (user_id, ts_code, name, trade_date, qty, avail_qty, current_price, cost_price,
             market_value, profit_amount, profit_pct, status, buy_date, source)
            VALUES ('tony', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'HOLDING', %s, 'TRADE')
        """, (ts_code, name, date.today(), data['current_qty'], data['current_qty'],
              current_price, data['avg_cost'], market_value, profit_amount, profit_pct, earliest_buy))
    
    conn.commit()
    cur.close(); conn.close()
    
    return {
        'ts_code': ts_code,
        'name': name,
        'qty': data['current_qty'],
        'avg_cost': data['avg_cost'],
        'current_price': current_price,
        'profit_pct': profit_pct,
        'avg_hold_days': data['avg_hold_days'],
        'market_value': market_value,
    }


def get_trade_history(ts_code=None, limit=50):
    """获取交易历史"""
    conn = get_conn()
    cur = conn.cursor()
    
    if ts_code:
        cur.execute("""
            SELECT * FROM trade_records WHERE ts_code=%s
            ORDER BY trade_date DESC, id DESC LIMIT %s
        """, (ts_code, limit))
    else:
        cur.execute("""
            SELECT * FROM trade_records
            ORDER BY trade_date DESC, id DESC LIMIT %s
        """, (limit,))
    
    tds = []
    for r in cur.fetchall():
        if isinstance(r, dict):
            tds.append({
                'id': r['id'], 'ts_code': r['ts_code'], 'name': r['name'],
                'trade_date': str(r['trade_date']), 'direction': r['direction'],
                'qty': r['qty'], 'price': float(r['price']), 'amount': float(r['amount']),
                'commission': float(r['commission']), 'notes': r['notes'] or '',
            })
        else:
            tds.append({
                'id': r[0], 'ts_code': r[1], 'name': r[2],
                'trade_date': str(r[3]), 'direction': r[4],
                'qty': r[5], 'price': float(r[6]), 'amount': float(r[7]),
                'commission': float(r[8]), 'notes': r[9] or '',
            })
    
    cur.close(); conn.close()
    return tds


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='交易记录管理系统')
    parser.add_argument('--init', action='store_true', help='初始化交易记录表')
    parser.add_argument('--add', action='store_true', help='添加交易记录')
    parser.add_argument('--code', type=str, help='股票代码')
    parser.add_argument('--name', type=str, default='', help='股票名称')
    parser.add_argument('--date', type=str, help='交易日期 YYYY-MM-DD')
    parser.add_argument('--dir', type=str, choices=['BUY', 'SELL'], help='方向 BUY/SELL')
    parser.add_argument('--qty', type=int, help='股数')
    parser.add_argument('--price', type=float, help='成交价')
    parser.add_argument('--calc', type=str, help='计算某只股票的加权持仓')
    parser.add_argument('--sync', type=str, help='同步某只股票到portfolio')
    parser.add_argument('--list', action='store_true', help='列出交易记录')
    args = parser.parse_args()
    
    if args.init:
        init_trade_table()
    
    if args.add:
        assert args.code and args.dir and args.qty and args.price
        d = args.date or str(date.today())
        tid = add_trade(args.code, args.name or '', d, args.dir, args.qty, args.price)
        print(f'✅ 交易记录已添加 (ID={tid})')
        result = sync_to_portfolio(args.code)
        if result:
            print(f'✅ 持仓已同步: {result["ts_code"]} {result["qty"]}股 '
                  f'成本¥{result["avg_cost"]} 盈亏{result["profit_pct"]:+.2f}%')
    
    if args.calc:
        result = calc_weighted_avg_hold_days(args.calc)
        print(f'📊 {result["ts_code"]}:')
        print(f'  当前持仓: {result["current_qty"]}股')
        print(f'  加权成本: ¥{result["avg_cost"]}') if result.get('avg_hold_days') is not None else None
        print(f'  加权持仓日: {result["avg_hold_days"]}日')
        print(f'  持仓批次: {result["lots"]}批')
    
    if args.sync:
        result = sync_to_portfolio(args.sync)
        if result:
            print(f'✅ 持仓已同步: {result["ts_code"]} {result["name"]} '
                  f'{result["qty"]}股 盈亏{result["profit_pct"]:+.2f}% 持仓{result["avg_hold_days"]}日')
    
    if args.list:
        tds = get_trade_history(args.code)
        print(f'📋 交易记录 ({len(tds)}笔):')
        for t in tds:
            d = t['direction']
            icon = '🟢 买入' if d == 'BUY' else '🔴 卖出'
            print(f'  {icon} {t["ts_code"]} {t["name"]} '
                  f'{t["trade_date"]} {t["qty"]}股 ¥{t["price"]:.2f}')
