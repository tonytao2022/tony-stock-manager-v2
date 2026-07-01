"""
routes/trade.py - 交易记录 CRUD
"""
import logging
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows

logger = logging.getLogger('trade_routes')
trade_bp = Blueprint('trade', __name__)


@trade_bp.route('/trade/records', methods=['GET'])
def list_trade_records():
    """获取交易记录"""
    try:
        ts_code = request.args.get('ts_code')
        limit = min(int(request.args.get('limit', 100)), 500)

        with db_cursor(commit=False) as cur:
            if ts_code:
                cur.execute("""
                    SELECT * FROM trade_records WHERE ts_code=%s 
                    ORDER BY trade_date DESC LIMIT %s
                """, (ts_code, limit))
            else:
                cur.execute("""
                    SELECT * FROM trade_records ORDER BY trade_date DESC LIMIT %s
                """, (limit,))
            rows = cur.fetchall()

        records = serialize_rows(rows, float_fields=['price', 'amount', 'commission'])
        for r in records:
            r['qty'] = int(r['qty'])
        return api_success({'records': records, 'total': len(records)})
    except Exception as e:
        return api_error(str(e))


@trade_bp.route('/trade/records', methods=['POST'])
def add_trade_record():
    """新增交易记录"""
    try:
        data = request.get_json() or {}
        ts_code = data.get('ts_code', '')
        name = data.get('name', '')
        trade_date = data.get('trade_date', '')
        direction = data.get('direction', 'BUY')
        qty = int(data.get('qty', 0))
        price = float(data.get('price', 0))
        commission = float(data.get('commission', 0) or 0)
        notes = (data.get('notes', '') or '')[:200]
        amount = round(qty * price, 2)

        if not ts_code or not qty or not price:
            return api_error('缺少必要参数')

        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO trade_records 
                (ts_code, name, trade_date, direction, qty, price, amount, commission, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (ts_code, name, trade_date, direction, qty, price, amount, commission, notes))

        # 同步持仓
        _sync_holding(ts_code, name)
        return api_success({'ts_code': ts_code, 'qty': qty, 'price': price})
    except Exception as e:
        return api_error(str(e))


@trade_bp.route('/trade/records/<int:record_id>', methods=['DELETE'])
def delete_trade_record(record_id):
    """删除交易记录"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT ts_code, name FROM trade_records WHERE id=%s", (record_id,))
            row = cur.fetchone()
            if not row:
                return api_error('记录不存在', code=404)

            cur.execute("DELETE FROM trade_records WHERE id=%s", (record_id,))
            _sync_holding(row['ts_code'], row.get('name', ''))
        return api_success({'deleted': record_id})
    except Exception as e:
        return api_error(str(e))


def _sync_holding(ts_code, name):
    """同步单只持仓：统计所有BUY/SELL记录，更新portfolio_holdings"""
    from db_config import get_connection
    from datetime import date
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # 计算净持仓
        cur.execute("""
            SELECT 
                COALESCE(SUM(CASE WHEN direction='BUY' THEN qty ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN direction='SELL' THEN qty ELSE 0 END), 0) as net_qty
            FROM trade_records WHERE ts_code=%s
        """, (ts_code,))
        r = cur.fetchone()
        net_qty = int(r['net_qty'] or 0)
        
        if net_qty <= 0:
            cur.execute("UPDATE portfolio_holdings SET shares=0, status='CLOSED' WHERE ts_code=%s AND status='HOLDING'", (ts_code,))
            conn.commit()
        else:
            # 计算加权平均成本
            cur.execute("""
                SELECT 
                    ROUND(SUM(CASE WHEN direction='BUY' THEN qty*price ELSE 0 END) / 
                          NULLIF(SUM(CASE WHEN direction='BUY' THEN qty ELSE 0 END), 0), 4) as avg_cost
                FROM trade_records WHERE ts_code=%s
            """, (ts_code,))
            r = cur.fetchone()
            avg_cost = float(r['avg_cost'] or 0)
            
            # 获取最新收盘价
            cur.execute("""
                SELECT close FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
            """, (ts_code,))
            r = cur.fetchone()
            current_price = float(r['close']) if r else avg_cost
            
            if avg_cost > 0:
                profit_pct = round((current_price - avg_cost) / avg_cost * 100, 2)
            else:
                profit_amount = (current_price - avg_cost) * net_qty
                profit_pct = round(profit_amount / abs(avg_cost * net_qty) * 100, 2) if avg_cost != 0 else 0
            
            market_value = round(current_price * net_qty, 2)
            profit_amount = round((current_price - avg_cost) * net_qty, 2)
            
            # 获取最早买入日期
            cur.execute("SELECT MIN(trade_date) as earliest FROM trade_records WHERE ts_code=%s AND direction='BUY'", (ts_code,))
            r = cur.fetchone()
            buy_date = r['earliest'] if r and r['earliest'] else date.today()
            
            # upsert
            cur.execute("""
                INSERT INTO portfolio_holdings (ts_code, name, shares, cost_price, current_price, 
                    market_value, profit_amount, profit_pct, status, buy_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'HOLDING', %s)
                ON DUPLICATE KEY UPDATE
                    shares=VALUES(shares), cost_price=VALUES(cost_price),
                    current_price=VALUES(current_price), market_value=VALUES(market_value),
                    profit_amount=VALUES(profit_amount), profit_pct=VALUES(profit_pct),
                    status='HOLDING', buy_date=VALUES(buy_date), updated_at=NOW()
            """, (ts_code, name, net_qty, avg_cost, current_price, market_value, profit_amount, profit_pct, buy_date))
            conn.commit()
        
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning(f'sync_holding {ts_code} 失败: {e}')
