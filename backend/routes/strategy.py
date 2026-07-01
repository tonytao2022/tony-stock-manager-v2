"""
routes/strategy.py - 策略信号
"""
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows
from auth import require_auth
from tool_registry import register_tool
from engines.strategy import check_all_holdings, check_single

strategy_bp = Blueprint('strategy', __name__)


@strategy_bp.route('/strategy/signals', methods=['GET'])
def list_signals():
    """获取策略信号列表"""
    try:
        limit = int(request.args.get('limit', 310))
        min_score = request.args.get('min_score', '')
        signal_type = request.args.get('signal_type', '')
        ts_code = request.args.get('ts_code', '')

        with db_cursor(commit=False) as cur:
            # 获取最新交易日
            cur.execute("SELECT MAX(ss.trade_date) as d FROM strategy_signal ss JOIN daily_kline dk ON ss.trade_date = dk.trade_date")
            row = cur.fetchone()
            trade_date = str(row['d']) if row and row['d'] else ''

            where = ["ss.trade_date=%s"]
            params = [trade_date]

            if min_score:
                where.append("ss.composite_score >= %s")
                params.append(float(min_score))
            if signal_type:
                where.append("ss.signal_type=%s")
                params.append(signal_type)
            if ts_code:
                where.append("ss.ts_code=%s")
                params.append(ts_code)

            if limit > 0:
                sql = f"""
                    SELECT ss.*, sb.name, sb.industry
                    FROM strategy_signal ss
                    LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                    WHERE {' AND '.join(where)}
                    ORDER BY ss.composite_score DESC
                    LIMIT %s
                """
                cur.execute(sql, params + [limit])
            else:
                sql = f"""
                    SELECT ss.*, sb.name, sb.industry
                    FROM strategy_signal ss
                    LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                    WHERE {' AND '.join(where)}
                    ORDER BY ss.composite_score DESC
                """
                cur.execute(sql, params)
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'signals': serialize_rows(rows),
        })
    except Exception as e:
        return api_error(str(e))


@strategy_bp.route('/strategy/checkpoints', methods=['GET'])
@require_auth
def checkpoints():
    """获取所有持仓的阶梯检查点状态"""
    try:
        results = check_all_holdings()
        return api_success({
            'count': len(results),
            'checkpoints': results,
        })
    except Exception as e:
        return api_error(str(e))


@strategy_bp.route('/strategy/check/<ts_code>', methods=['GET'])
@require_auth
def check_single_holding(ts_code):
    """获取单只持仓的检查点状态"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT * FROM portfolio_holdings WHERE ts_code=%s LIMIT 1
            """, [ts_code])
            holding = cur.fetchone()

        if not holding:
            return api_error(f'未找到持仓: {ts_code}', code=404)

        result = check_single(holding)
        return api_success(result)
    except Exception as e:
        return api_error(str(e))


@strategy_bp.route('/strategy/config', methods=['GET'])
def get_config():
    """获取策略配置（季节参数矩阵）"""
    try:
        from db_config import db_cursor, api_success, api_error
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT id, name, season_type, buy_min_score, max_hold_days, stop_loss_pct,
                       trailing_stop_pct, max_pos_pct, max_total_pct, position_tolerance, cool_days, p1_score, p2_score, p3_score
                FROM strategy_config WHERE is_active=1 AND season_type IS NOT NULL AND season_type != ''
            """)
            rows = cur.fetchall()

        # 转为两种格式：configDict（首页系统设置用）+ configArray（backtest.html用）
        configDict = {}
        configArray = []
        for r in rows:
            sea = r['season_type']
            t1 = float(r['stop_loss_pct']) if r['stop_loss_pct'] else 10
            configDict[sea] = {
                'line': int(r['buy_min_score'] or 50),
                'hold': int(r['max_hold_days'] or 30),
                't1': t1,
                't2': t1 - 3 if t1 > 3 else t1,
                'trail': float(r['trailing_stop_pct'] or 12),
                'maxpos': 8,
                'posPct': int(r['max_pos_pct'] or 15),
            }
            configArray.append({
                'id': r.get('id'),
                'name': r.get('name'),
                'season_type': sea,
                'buy_min_score': r['buy_min_score'],
                'max_pos_pct': r['max_pos_pct'],
                'max_total_pct': r.get('max_total_pct', 30),
                'position_tolerance': r.get('position_tolerance', 5),
                'stop_loss_pct': float(r['stop_loss_pct']) if r['stop_loss_pct'] else 0,
                'max_hold_days': r['max_hold_days'],
                'trailing_stop_pct': float(r['trailing_stop_pct']) if r['trailing_stop_pct'] else 0,
                'cool_days': r.get('cool_days') or 15,
                'p1_score': r.get('p1_score') or 40,
                'p2_score': r.get('p2_score') or 30,
                'p3_score': r.get('p3_score') or 20,
            })
        return api_success({'config': configDict, 'items': configArray})
    except Exception as e:
        return api_error(str(e))


@strategy_bp.route('/strategy/config', methods=['PUT'])
@require_auth
def update_config():
    """更新策略配置"""
    try:
        data = request.get_json()
        if not data or 'config_key' not in data or 'config_value' not in data:
            return api_error('缺少 config_key 或 config_value')

        with db_cursor() as cur:
            cur.execute("""
                UPDATE strategy_config SET config_value=%s WHERE config_key=%s
            """, [data['config_value'], data['config_key']])

        return api_success({'config_key': data['config_key']}, '更新成功')
    except Exception as e:
        return api_error(str(e))

@strategy_bp.route('/strategy/versions', methods=['GET'])
def list_versions():
    """策略版本列表"""
    from db_config import db_cursor, api_success, api_error, serialize_rows
    from collections import defaultdict
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT version, version_name, change_desc, created_by, created_at
                FROM strategy_config_versions
                GROUP BY version, version_name, change_desc, created_by, created_at
                ORDER BY version DESC
            """)
            rows = cur.fetchall()
        
        versions = []
        seen = set()
        for r in rows:
            ver = r['version']
            if ver in seen:
                continue
            seen.add(ver)
            versions.append({
                'version': ver,
                'name': r['version_name'] or ('V'+str(ver)),
                'desc': r['change_desc'] or '',
                'created_by': r['created_by'] or 'system',
                'created_at': str(r['created_at']) if r['created_at'] else '',
            })
        return api_success({'versions': versions})
    except Exception as e:
        return api_error(str(e))


@strategy_bp.route('/strategy/version/<int:ver>/apply', methods=['POST'])
def apply_version(ver):
    """应用策略版本：将版本快照写入strategy_config"""
    from db_config import db_cursor, api_success, api_error
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT config_id, snapshot FROM strategy_config_versions WHERE version=%s
            """, (ver,))
            rows = cur.fetchall()
            if not rows:
                return api_error('版本不存在')
            
            import json
            applied = 0
            for r in rows:
                snap = json.loads(r['snapshot']) if isinstance(r['snapshot'], str) else r['snapshot']
                if snap.get('id', 0) <= 0:
                    continue
                cur.execute("""
                    UPDATE strategy_config SET
                        buy_min_score=%s, max_pos_pct=%s, stop_loss_pct=%s,
                        max_hold_days=%s, cool_days=%s, trailing_stop_pct=%s,
                        p1_score=%s, p2_score=%s, p3_score=%s
                    WHERE id=%s
                """, (
                    snap.get('buy_min_score', 50), snap.get('max_pos_pct', 20),
                    snap.get('stop_loss', 12), snap.get('max_hold', 30),
                    snap.get('cool_days', 15), snap.get('trailing_stop', 18),
                    snap.get('p1', 40), snap.get('p2', 30), snap.get('p3', 20),
                    snap['id']
                ))
                applied += 1
        return api_success({'applied': ver, 'configs_updated': applied})
    except Exception as e:
        return api_error(str(e))


@strategy_bp.route('/strategy/version/snapshot', methods=['POST'])
def snapshot_version():
    """创建策略快照：将当前strategy_config保存为新版本"""
    from db_config import db_cursor, api_success, api_error, serialize_rows
    import json
    try:
        with db_cursor(commit=False) as cur:
            # 取当前最大版本号
            cur.execute("SELECT MAX(version) as mv FROM strategy_config_versions")
            row = cur.fetchone()
            next_ver = (row['mv'] or 0) + 1
            
            # 取当前所有active配置
            cur.execute("""
                SELECT * FROM strategy_config WHERE is_active=1 ORDER BY id
            """)
            configs = cur.fetchall()
            
            # 逐个写入快照
            for cfg in configs:
                snap = {
                    'id': cfg['id'],
                    'name': cfg['name'],
                    'buy_min_score': cfg['buy_min_score'],
                    'max_pos_pct': cfg['max_pos_pct'],
                    'stop_loss': float(cfg['stop_loss_pct']),
                    'max_hold': cfg['max_hold_days'],
                    'cool_days': cfg['cool_days'],
                    'trailing_stop': float(cfg['trailing_stop_pct']),
                    'p1': cfg['p1_score'],
                    'p2': cfg['p2_score'],
                    'p3': cfg['p3_score'],
                    'description': cfg['description'],
                    'season_type': cfg['season_type'],
                    'is_active': True,
                }
                cur.execute("""
                    INSERT INTO strategy_config_versions 
                    (version, version_name, config_id, snapshot, change_desc, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (next_ver, f'V{next_ver}', cfg['id'], json.dumps(snap, ensure_ascii=False),
                       '当前V12.2配置快照', 'system'))
        return api_success({'version': next_ver, 'snapshot': True})
    except Exception as e:
        return api_error(str(e))
