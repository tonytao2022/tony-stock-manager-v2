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


# ===== 评分 =====

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
        SELECT s.ts_code, COALESCE(w.name, CONVERT(s.ts_code USING utf8mb4)) AS name,
               w.industry,
               s.tide_score, s.l3_score, s.chanlun_bonus,
               s.tide_track, s.tide_label,
               c.central_breakthrough, c.divergence, c.third_buy
        FROM tide_score_signal s
        LEFT JOIN tide_chanlun_signal c ON s.ts_code=c.ts_code AND s.trade_date=c.trade_date
        LEFT JOIN watch_pool w ON CONVERT(s.ts_code USING utf8mb4) = CONVERT(w.ts_code USING utf8mb4)
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


# ===== 对比 =====

@tide_bp.route('/api/v3/tide-compare')
def api_tide_compare():
    """Tide vs V4 评分对比"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) as d FROM tide_score_signal")
    row = cur.fetchone()
    tide_date = str(row['d']) if row and row['d'] else None
    if not tide_date:
        cur.close(); conn.close()
        return jsonify({'code': 404, 'message': '暂无Tide数据'})

    cur.execute("""
        SELECT s.ts_code, COALESCE(w.name, CONVERT(s.ts_code USING utf8mb4)) AS name,
               w.industry,
               s.tide_score, s.l3_score, s.chanlun_bonus, s.tide_track, s.tide_label
        FROM tide_score_signal s
        LEFT JOIN watch_pool w ON CONVERT(s.ts_code USING utf8mb4) = CONVERT(w.ts_code USING utf8mb4)
        WHERE s.trade_date=%s
    """, (tide_date,))
    tide_map = {}
    for r in cur.fetchall():
        tide_map[r['ts_code']] = {
            'name': r['name'],
            'industry': r['industry'] if r['industry'] else '',
            'tide_score': float(r['tide_score']),
            'l3_score': float(r['l3_score']) if r['l3_score'] else 0,
            'bonus': float(r['chanlun_bonus']) if r['chanlun_bonus'] else 0,
            'track': r['tide_track'],
            'label': r['tide_label']
        }

    # 从 daily_score_snapshot 取 V4 实时评分（与 V2 页面一致）
    cur.execute("""
        SELECT MAX(trade_date) as d FROM daily_score_snapshot
        WHERE trade_date <= %s
    """, (tide_date,))
    v4_date_row = cur.fetchone()
    v4_date = str(v4_date_row['d']) if v4_date_row and v4_date_row['d'] else None
    v4_map = {}
    if v4_date:
        cur.execute("""
            SELECT ts_code, composite_score FROM daily_score_snapshot 
            WHERE trade_date=%s AND composite_score IS NOT NULL
        """, (v4_date,))
        for r in cur.fetchall():
            v4_map[r['ts_code']] = {
                'total_score': float(r['composite_score']),
                'track': 'momentum'
            }
    cur.close(); conn.close()

    common = [c for c in tide_map if c in v4_map]
    compare_rows = []
    for code in common:
        compare_rows.append({
            'ts_code': code,
            'name': tide_map[code].get('name', code),
            'industry': tide_map[code].get('industry', ''),
            'tide_score': tide_map[code]['tide_score'],
            'tide_l3': tide_map[code]['l3_score'],
            'tide_bonus': tide_map[code]['bonus'],
            'tide_track': tide_map[code]['track'],
            'tide_label': tide_map[code]['label'],
            'v4_score': v4_map[code]['total_score'],
            'v4_track': v4_map[code]['track'],
            'diff': round(tide_map[code]['tide_score'] - v4_map[code]['total_score'], 2)
        })

    tide_scores = [c['tide_score'] for c in compare_rows]
    v4_scores = [c['v4_score'] for c in compare_rows]
    n = len(compare_rows)
    if n > 0:
        t_mean = sum(tide_scores) / n
        v_mean = sum(v4_scores) / n
        t_var = sum((x - t_mean)**2 for x in tide_scores) / n
        v_var = sum((x - v_mean)**2 for x in v4_scores) / n
        var_ratio = round(t_var / v_var, 2) if v_var > 0 else 999
    else:
        t_mean = v_mean = t_var = v_var = var_ratio = 0
    stats = {
        'tide_date': tide_date,
        'v4_date': v4_date,
        'common_count': n,
        'tide_mean': round(t_mean, 2),
        'v4_mean': round(v_mean, 2),
        'tide_variance': round(t_var, 2),
        'v4_variance': round(v_var, 2),
        'var_ratio': var_ratio,
        'var_ratio_pass_tc2': var_ratio >= 1.2
    }
    return jsonify({'code': 200, 'data': compare_rows, 'stats': stats})


@tide_bp.route('/api/v3/tide-backtest-compare')
def api_tide_backtest_compare():
    """Tide回测对比结果"""
    run_id = request.args.get('run_id')
    conn = get_connection()
    cur = conn.cursor()
    if run_id:
        cur.execute("""
            SELECT * FROM tide_backtest_result WHERE run_id=%s
            ORDER BY trade_date, ts_code
        """, (run_id,))
    else:
        cur.execute("SELECT MAX(run_id) as rid FROM tide_backtest_result")
        row = cur.fetchone()
        if row and row['rid']:
            cur.execute("""
                SELECT * FROM tide_backtest_result WHERE run_id=%s
                ORDER BY trade_date, ts_code
            """, (row['rid'],))
        else:
            cur.close(); conn.close()
            return jsonify({'code': 404, 'message': '暂无回测数据'})
    rows = cur.fetchall()
    cur.close(); conn.close()
    data = [dict(r) for r in rows]
    for d in data:
        d.pop('id', None); d.pop('created_at', None)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 4)
    return jsonify({'code': 200, 'data': data, 'count': len(data)})


# ===== IC验证 =====

@tide_bp.route('/api/v3/tide-ic-report')
def api_tide_ic_report():
    """IC验证报告"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT config_key, config_value, updated_at FROM tide_config 
        WHERE config_key LIKE 'ic_%' ORDER BY config_key
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    data = {}
    for r in rows:
        key = r['config_key']
        try:
            data[key] = json.loads(r['config_value'])
            data[key]['updated_at'] = str(r['updated_at'])
        except:
            data[key] = {'raw': r['config_value']}
    return jsonify({'code': 200, 'data': data})


@tide_bp.route('/api/v3/tide-ic-run', methods=['POST'])
def api_tide_ic_run():
    """手动触发IC验证"""
    try:
        from tide_engine.tide_ic_validate import run_ic_validation
        result = run_ic_validation()
        return jsonify({'code': 200, 'message': 'IC验证完成', 'data': result})
    except Exception as e:
        logger.error(f'IC验证失败: {e}')
        return jsonify({'code': 500, 'message': str(e)}), 500


@tide_bp.route('/api/v3/tide-backtest-run', methods=['POST'])
def api_tide_backtest_run():
    """触发Tide回测"""
    try:
        from tide_engine.tide_backtest_v2 import run_backtest
        result = run_backtest()
        return jsonify({'code': 200, 'message': '回测完成', 'data': result})
    except Exception as e:
        logger.error(f'Tide回测失败: {e}')
        return jsonify({'code': 500, 'message': str(e)}), 500


# ===== 配置 =====

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
