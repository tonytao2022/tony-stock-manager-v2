"""
routes/holdings.py - 持仓管理 CRUD
"""
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows
from auth import require_auth

holdings_bp = Blueprint('holdings', __name__)


@holdings_bp.route('/holdings', methods=['GET'])
@require_auth
def list_holdings():
    """持仓列表"""
    try:
        status = request.args.get('status', '')

        with db_cursor(commit=False) as cur:
            sql = """
                SELECT ph.*, ss.composite_score, ss.signal_type
                FROM portfolio_holdings ph
                LEFT JOIN strategy_signal ss ON ph.ts_code = ss.ts_code
                    AND ss.trade_date = (SELECT MAX(ss2.trade_date) FROM strategy_signal ss2 JOIN daily_kline dk ON ss2.trade_date = dk.trade_date)
            """
            params = []
            if status:
                sql += " WHERE ph.status=%s"
                params.append(status)
            sql += " ORDER BY ph.created_at DESC"

            cur.execute(sql, params)
            rows = cur.fetchall()

        NUM_FIELDS = ['cost_price', 'current_price', 'market_value', 'profit_pct', 
                'profit_amount', 'position_ratio', 'composite_score']
        return api_success({'holdings': serialize_rows(rows, float_fields=NUM_FIELDS)})
    except Exception as e:
        return api_error(str(e))


@holdings_bp.route('/holdings', methods=['POST'])
@require_auth
def add_holding():
    """新增持仓"""
    try:
        data = request.get_json()
        if not data:
            return api_error('缺少请求数据')

        ts_code = data.get('ts_code')
        name = data.get('name')
        shares = int(data.get('shares', 0))
        cost_price = float(data.get('cost_price', 0))
        buy_date = data.get('buy_date')

        if not all([ts_code, name, shares > 0]):
            return api_error('缺少必填字段: ts_code, name, shares')

        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO portfolio_holdings
                    (ts_code, name, shares, cost_price, buy_date, status)
                VALUES (%s, %s, %s, %s, %s, 'hold')
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name),
                    shares=shares+VALUES(shares),
                    cost_price=(cost_price*shares+VALUES(cost_price)*VALUES(shares))/(shares+VALUES(shares))
            """, [ts_code, name, shares, cost_price, buy_date])

        return api_success({'ts_code': ts_code, 'name': name}, '新增成功')
    except Exception as e:
        return api_error(str(e))


@holdings_bp.route('/holdings/<ts_code>', methods=['PUT'])
@require_auth
def update_holding(ts_code):
    """更新持仓"""
    try:
        data = request.get_json()
        if not data:
            return api_error('缺少请求数据')

        updates = []
        params = []

        for field in ['shares', 'cost_price', 'current_price', 'status',
                       'position_ratio', 'profit_pct', 'profit_amount',
                       'market_value', 'lock_reason', 'notes', 'buy_date', 'name']:
            if field in data:
                updates.append(f"{field}=%s")
                params.append(data[field])

        if not updates:
            return api_error('无更新字段')

        updates.append("updated_at=NOW()")
        params.append(ts_code)

        with db_cursor() as cur:
            cur.execute(
                f"UPDATE portfolio_holdings SET {', '.join(updates)} WHERE ts_code=%s",
                params
            )

        return api_success({'ts_code': ts_code}, '更新成功')
    except Exception as e:
        return api_error(str(e))


@holdings_bp.route('/holdings/<ts_code>', methods=['DELETE'])
@require_auth
def delete_holding(ts_code):
    """删除持仓"""
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM portfolio_holdings WHERE ts_code=%s", [ts_code])

        return api_success({'ts_code': ts_code}, '删除成功')
    except Exception as e:
        return api_error(str(e))


@holdings_bp.route('/holdings/calc', methods=['GET'])
@require_auth
def calc_holdings():
    """计算所有持仓的当前盈亏"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT id, ts_code, name, shares, cost_price
                FROM portfolio_holdings
                WHERE status IN ('HOLDING', 'hold', 'locked')
            """)
            holdings = cur.fetchall()

        total_value = 0
        total_profit = 0
        results = []

        for h in holdings:
            ts_code = h['ts_code']
            shares = int(h['shares'])
            cost = float(h['cost_price'])

            # 获取最新价格
            with db_cursor(commit=False) as cur:
                cur.execute("""
                    SELECT close FROM daily_kline
                    WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
                """, [ts_code])
                kline = cur.fetchone()

            current = float(kline['close']) if kline else cost
            market_value = round(current * shares, 2)
            profit_amount = round((current - cost) * shares, 2)
            profit_pct = round((current - cost) / cost * 100, 2) if cost > 0 else 0
            # 成本为负数时，盈亏率 = (现价 - 成本) / 成本 会算反
            # 此时直接用盈亏金额/绝对值(成本额×持股数) 更准确
            if cost <= 0 and shares > 0:
                cost_value = cost * shares
                if cost_value < 0:
                    profit_pct = round(profit_amount / abs(cost_value) * 100, 2)

            total_value += market_value
            total_profit += profit_amount

            results.append({
                'ts_code': ts_code,
                'name': h['name'],
                'shares': shares,
                'cost_price': cost,
                'current_price': current,
                'market_value': market_value,
                'profit_amount': profit_amount,
                'profit_pct': profit_pct,
            })

            # 更新数据库
            with db_cursor() as cur:
                cur.execute("""
                    UPDATE portfolio_holdings SET
                        current_price=%s, market_value=%s,
                        profit_pct=%s, profit_amount=%s
                    WHERE id=%s
                """, [current, market_value, profit_pct, profit_amount, h['id']])

        return api_success({
            'holdings': results,
            'total_value': round(total_value, 2),
            'total_profit': round(total_profit, 2),
        })
    except Exception as e:
        return api_error(str(e))
