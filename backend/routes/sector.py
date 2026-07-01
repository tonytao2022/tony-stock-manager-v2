"""
routes/sector.py - 板块轮动
"""
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows
sector_bp = Blueprint('sector', __name__)


@sector_bp.route('/sector/top', methods=['GET'])
def sector_top():
    """板块涨幅排行（含趋势和股票数）"""
    try:
        limit = min(int(request.args.get('limit', 10)), 30)

        with db_cursor(commit=False) as cur:
            cur.execute("SELECT MAX(trade_date) as d FROM sector_index_daily")
            row = cur.fetchone()
            trade_date = str(row['d']) if row and row['d'] else ''

            if trade_date:
                cur.execute("""
                    SELECT sid.sector_code,
                           COALESCE(sm.sector_name, sid.sector_code) as sector_name,
                           sid.change_pct, sid.close
                    FROM sector_index_daily sid
                    LEFT JOIN sector_mapping sm ON sid.sector_code = sm.sector_code
                    WHERE sid.trade_date=%s
                    GROUP BY sid.sector_code, sm.sector_name, sid.change_pct, sid.close
                    ORDER BY ABS(sid.change_pct) DESC LIMIT %s
                """, [trade_date, limit])
                index_sectors = cur.fetchall()
                
                # 补充股票数和趋势
                for s in index_sectors:
                    cur.execute("SELECT COUNT(*) as c FROM stock_basic WHERE industry=%s AND is_active=1", (s['sector_code'],))
                    s['stock_count'] = cur.fetchone()['c']
                    
                    pct = float(s.get('change_pct') or 0)
                    if pct > 2: s['trend_type'] = 'up'
                    elif pct > 0: s['trend_type'] = 's_up'
                    elif pct > -2: s['trend_type'] = 's_down'
                    else: s['trend_type'] = 'down'
            else:
                index_sectors = []

        return api_success({
            'trade_date': trade_date,
            'sectors': serialize_rows(index_sectors, float_fields=['change_pct','close']),
        })
    except Exception as e:
        return api_error(str(e))


@sector_bp.route('/sector/detail', methods=['GET'])
def sector_detail():
    """板块内个股详情"""
    try:
        sector = request.args.get('sector', '')
        trade_date = request.args.get('date', '')

        if not sector:
            return api_error('缺少 sector 参数')

        with db_cursor(commit=False) as cur:
            if not trade_date:
                cur.execute("SELECT MAX(trade_date) as d FROM daily_kline")
                row = cur.fetchone()
                trade_date = str(row['d']) if row and row['d'] else ''

            cur.execute("""
                SELECT ss.ts_code, sb.name, sb.industry,
                       ss.composite_score, ss.signal_type,
                       dk.close, dk.change_pct
                FROM strategy_signal ss
                JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                LEFT JOIN daily_kline dk ON ss.ts_code = dk.ts_code AND dk.trade_date=%s
                WHERE sb.industry=%s AND ss.trade_date=%s
                ORDER BY ss.composite_score DESC LIMIT 20
            """, [trade_date, sector, trade_date])
            stocks = cur.fetchall()

        return api_success({
            'sector': sector,
            'trade_date': trade_date,
            'count': len(stocks),
            'stocks': serialize_rows(stocks),
        })
    except Exception as e:
        return api_error(str(e))
