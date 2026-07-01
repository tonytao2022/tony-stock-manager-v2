"""
routes/backtest.py - 回测路由（V2.0完整版）
"""
from flask import Blueprint, request
from datetime import date, timedelta
from db_config import api_success, api_error
from auth import require_auth
from engines.backtest_ladder import (
    run_ladder_backtest, list_pool, manage_pool,
    DEFAULT_PARAMS
)

backtest_bp = Blueprint('backtest', __name__)


@backtest_bp.route('/backtest/run', methods=['POST'])
@require_auth
def bt_run():
    """阶梯策略回测"""
    try:
        data = request.get_json() or {}
        params = {}
        for k in DEFAULT_PARAMS:
            if k in data:
                params[k] = data[k]

        pool_only = data.get('pool_only', True)

        report = run_ladder_backtest(params=params, pool_only=pool_only)
        if report is None:
            return api_error('回测失败：无数据')

        return api_success(report)
    except Exception as e:
        return api_error(str(e))


@backtest_bp.route('/backtest/pool', methods=['GET'])
@require_auth
def bt_pool():
    """回测池列表"""
    try:
        stocks = list_pool()
        return api_success({'stocks': stocks, 'total': len(stocks)})
    except Exception as e:
        return api_error(str(e))


@backtest_bp.route('/backtest/pool', methods=['POST'])
@require_auth
def bt_pool_manage():
    """回测池管理"""
    try:
        data = request.get_json() or {}
        action = data.get('action', '')
        ts_code = data.get('ts_code', '')
        name = data.get('name', '')
        industry = data.get('industry', '')

        if not action or not ts_code:
            return api_error('缺少参数: action, ts_code')

        result = manage_pool(action, ts_code, name, industry)
        if result is None:
            return api_error('操作失败')

        return api_success(result)
    except Exception as e:
        return api_error(str(e))


@backtest_bp.route('/backtest/reports', methods=['GET'])
@require_auth
def bt_reports():
    """获取历史回测报告列表"""
    try:
        from engines.backtest import list_reports
        limit = min(int(request.args.get('limit', 10)), 50)
        reports = list_reports(limit)
        return api_success({'reports': reports})
    except Exception as e:
        return api_error(str(e))


@backtest_bp.route('/backtest/report/<int:report_id>', methods=['GET'])
@require_auth
def bt_report_detail(report_id):
    """回测报告详情"""
    try:
        from engines.backtest import get_report_detail
        detail = get_report_detail(report_id)
        if not detail:
            return api_error('报告不存在', code=404)
        return api_success(detail)
    except Exception as e:
        return api_error(str(e))

@backtest_bp.route('/backtest/delete/<int:report_id>', methods=['POST', 'DELETE'])
@require_auth
def bt_delete_report(report_id):
    """删除指定回测报告及关联交易记录"""
    from db_config import db_cursor, api_success, api_error
    try:
        with db_cursor() as cur:
            # 先删除关联的交易记录（如果存在单独的 trade_records 表）
            cur.execute("DELETE FROM backtest_trade WHERE report_id=%s", (report_id,))
            # 删除回测报告
            cur.execute("DELETE FROM backtest_report WHERE id=%s", (report_id,))
            affected = cur.rowcount
        if affected == 0:
            return api_error('报告不存在', code=404)
        return api_success({'deleted': True, 'id': report_id})
    except Exception as e:
        return api_error(str(e))


@backtest_bp.route('/backtest/history', methods=['GET'])
def backtest_history():
    """回测历史记录"""
    from db_config import db_cursor, api_success, api_error, serialize_rows
    import json
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT id, report_date, strategy_name, total_trades, win_trades, lose_trades,
                       win_rate, avg_win_pct, avg_lose_pct, profit_factor, max_drawdown,
                       total_return, avg_hold_days
                FROM backtest_report ORDER BY report_date DESC
            """)
            rows = cur.fetchall()
        runs = serialize_rows(rows, float_fields=[
            'win_rate','avg_win_pct','avg_lose_pct','profit_factor',
            'max_drawdown','total_return','avg_hold_days'
        ])
        return api_success({'runs': runs, 'total': len(runs)})
    except Exception as e:
        return api_error(str(e))


@backtest_bp.route('/backtest/report/<int:report_id>', methods=['GET'])
def backtest_report_detail(report_id):
    """回测报告详情"""
    from db_config import db_cursor, api_success, api_error, serialize_rows
    import json as _j
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM backtest_report WHERE id=%s", (report_id,))
            row = cur.fetchone()
        if not row:
            return api_error('报告不存在', code=404)
        report = dict(row)
        for k in ['win_rate','avg_win_pct','avg_lose_pct','profit_factor','max_drawdown','total_return','avg_hold_days']:
            if report.get(k) is not None:
                report[k] = float(report[k])
        if report.get('trade_records'):
            if isinstance(report['trade_records'], str):
                report['trade_records'] = _j.loads(report['trade_records'])
            elif isinstance(report['trade_records'], bytes):
                report['trade_records'] = _j.loads(report['trade_records'].decode())
        return api_success(report)
    except Exception as e:
        return api_error(str(e))
