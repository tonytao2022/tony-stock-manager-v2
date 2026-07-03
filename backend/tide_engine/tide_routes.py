#!/usr/bin/env python3
"""
tide_routes.py - Tide评分引擎API路由
注册到 v2_unified_api.py，路由前缀 /api/v3/tide-*
"""
import os, sys, json, logging
from flask import Blueprint, request, jsonify

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection
from tide_engine.tide_scorer import run_scoring

logger = logging.getLogger('tide_routes')
tide_bp = Blueprint('tide', __name__)


@tide_bp.route('/api/v3/tide-score-run', methods=['POST'])
def api_tide_score_run():
    """手动触发Tide评分"""
    try:
        from datetime import date
        result = run_scoring()
        return jsonify({'code': 200, 'message': '评分完成', 'data': result})
    except Exception as e:
        logger.error(f'Tide评分失败: {e}')
        return jsonify({'code': 500, 'message': str(e)}), 500


@tide_bp.route('/api/v3/tide-score-list')
def api_tide_score_list():
    """Tide评分列表"""
    trade_date = request.args.get('date')
    if not trade_date:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) as d FROM tide_score_signal")
        row = cur.fetchone()
        trade_date = str(row['d']) if row and row['d'] else None
        cur.close(); conn.close()
    if not trade_date:
        return jsonify({'code': 404, 'message': '暂无数据'})
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.ts_code, s.tide_score, s.l3_score, s.chanlun_bonus,
               s.tide_track, s.tide_label,
               c.central_breakthrough, c.divergence, c.third_buy
        FROM tide_score_signal s
        LEFT JOIN tide_chanlun_signal c ON s.ts_code=c.ts_code AND s.trade_date=c.trade_date
        WHERE s.trade_date=%s
        ORDER BY s.tide_score DESC
    """, (trade_date,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({'code': 200, 'data': rows, 'total': len(rows), 'trade_date': trade_date})


@tide_bp.route('/api/v3/tide-factor-detail')
def api_tide_factor_detail():
    """7因子明细"""
    ts_code = request.args.get('ts_code')
    trade_date = request.args.get('date')
    if not trade_date:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) as d FROM tide_factor_value")
        row = cur.fetchone()
        trade_date = str(row['d']) if row and row['d'] else None
        cur.close(); conn.close()
    if not ts_code or not trade_date:
        return jsonify({'code': 400, 'message': '需要ts_code和date'})
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM tide_factor_value
        WHERE ts_code=%s AND trade_date=%s
    """, (ts_code, trade_date))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        d = dict(row)
        d.pop('id', None); d.pop('created_at', None)
        return jsonify({'code': 200, 'data': d})
    return jsonify({'code': 404, 'message': '未找到'})


@tide_bp.route('/api/v3/tide-chanlun')
def api_tide_chanlun():
    """缠论信号"""
    ts_code = request.args.get('ts_code')
    trade_date = request.args.get('date')
    if not trade_date:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) as d FROM tide_chanlun_signal")
        row = cur.fetchone()
        trade_date = str(row['d']) if row and row['d'] else None
        cur.close(); conn.close()
    if not trade_date:
        return jsonify({'code': 404, 'message': '暂无数据'})
    conn = get_connection()
    cur = conn.cursor()
    if ts_code:
        cur.execute("""
            SELECT * FROM tide_chanlun_signal
            WHERE ts_code=%s AND trade_date=%s
        """, (ts_code, trade_date))
    else:
        cur.execute("""
            SELECT * FROM tide_chanlun_signal WHERE trade_date=%s
            ORDER BY ts_code
        """, (trade_date,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({'code': 200, 'data': [dict(r) for r in rows]})


@tide_bp.route('/api/v3/tide-config', methods=['GET', 'POST'])
def api_tide_config():
    """Tide配置管理"""
    conn = get_connection()
    cur = conn.cursor()
    if request.method == 'POST':
        data = request.json
        if not data:
            return jsonify({'code': 400, 'message': '需要config_key和config_value'})
        cur.execute("""
            INSERT INTO tide_config (config_key, config_value, description)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)
        """, (data['config_key'], data['config_value'], data.get('description', '')))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'code': 200, 'message': '配置已更新'})
    cur.execute("SELECT config_key, config_value, description FROM tide_config")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({'code': 200, 'data': [dict(r) for r in rows]})
