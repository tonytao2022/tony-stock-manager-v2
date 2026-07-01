"""
routes/analysis.py - 个股分析
"""
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows
from auth import require_auth

analysis_bp = Blueprint('analysis', __name__)


@analysis_bp.route('/stock/search', methods=['GET'])
@require_auth
def search_stock():
    """搜索股票"""
    try:
        keyword = request.args.get('keyword', '')

        if not keyword or len(keyword) < 1:
            return api_success({'stocks': []})

        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ts_code, name, industry, market
                FROM stock_basic
                WHERE name LIKE %s OR ts_code LIKE %s
                LIMIT 20
            """, [f'%{keyword}%', f'%{keyword}%'])
            rows = cur.fetchall()

        return api_success({'stocks': serialize_rows(rows)})
    except Exception as e:
        return api_error(str(e))


@analysis_bp.route('/stock/<ts_code>', methods=['GET'])
@require_auth
def stock_detail(ts_code):
    """股票详情（基本信息+最新评分+K线）"""
    try:
        with db_cursor(commit=False) as cur:
            # 基本信息
            cur.execute("""
                SELECT * FROM stock_basic WHERE ts_code=%s LIMIT 1
            """, [ts_code])
            basic = cur.fetchone()

            # 最新评分
            cur.execute("""
                SELECT * FROM strategy_signal
                WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
            """, [ts_code])
            signal = cur.fetchone()

            # 最近30根K线
            cur.execute("""
                SELECT trade_date, open, high, low, close, change_pct, vol
                FROM daily_kline
                WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 30
            """, [ts_code])
            klines = cur.fetchall()

            # 备注
            cur.execute("""
                SELECT * FROM stock_notes
                WHERE ts_code=%s ORDER BY created_at DESC LIMIT 5
            """, [ts_code])
            notes = cur.fetchall()

        return api_success({
            'basic': serialize_rows([basic])[0] if basic else None,
            'signal': serialize_rows([signal])[0] if signal else None,
            'klines': serialize_rows(klines),
            'notes': serialize_rows(notes),
        })
    except Exception as e:
        return api_error(str(e))


@analysis_bp.route('/stock/<ts_code>/finance', methods=['GET'])
@require_auth
def stock_finance(ts_code):
    """股票财务汇总"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT dk.ts_code, sb.name, sb.industry,
                       dk.close, dk.change_pct, dk.vol, dk.amount,
                       dk.high, dk.low, dk.pre_close
                FROM daily_kline dk
                JOIN stock_basic sb ON dk.ts_code = sb.ts_code
                WHERE dk.ts_code=%s
                ORDER BY dk.trade_date DESC LIMIT 1
            """, [ts_code])
            finance = cur.fetchone()

        return api_success(serialize_rows([finance])[0] if finance else {})
    except Exception as e:
        return api_error(str(e))
