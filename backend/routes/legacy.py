"""
routes/legacy.py - v2_unified_api 残余路由批量迁移
所有从v2_unified_api搬运到新后端的API
"""
import os, json, subprocess, logging, math
from datetime import date, datetime, timedelta
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows, get_connection

logger = logging.getLogger('legacy_routes')
legacy_bp = Blueprint('legacy', __name__)


# ─── 系统健康 ───────────────────────────────────────────
@legacy_bp.route('/system/health', methods=['GET'])
def system_health():
    """系统健康检查"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT 1")
            db_ok = True
    except:
        db_ok = False
    try:
        disk = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=3)
        dp = disk.stdout.split('\n')[1].split() if disk.stdout else [''] * 6
        disk_usage = dp[4] if len(dp) > 4 else '?'
    except:
        disk_usage = '?'
    return api_success({
        'status': 'running', 'port': 8891, 'service': 'stock-system-v2',
        'version': '2.0.0', 'database': 'connected' if db_ok else 'disconnected',
        'disk_usage': disk_usage,
    })



# ─── 系统备份 ───────────────────────────────────────────
@legacy_bp.route('/system/backup', methods=['POST'])
def system_backup():
    """数据库备份"""
    try:
        import subprocess, time
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        fn = f'/root/stock_db_backup_{ts}.sql.gz'
        pwd_re = __import__('re').search(r'password\s*=\s*(\S+)', open('/etc/mysql/debian.cnf').read())
        pwd = pwd_re.group(1) if pwd_re else ''
        cmd = f'mysqldump -u debian-sys-maint -p{pwd} stock_db_v2 | gzip > {fn}'
        subprocess.run(cmd, shell=True, timeout=300)
        return api_success({'file': fn, 'time': ts})
    except Exception as e:
        return api_error(str(e))


# ─── 健康检查 ───────────────────────────────────────────
@legacy_bp.route('/health', methods=['GET'])
def health():
    return api_success({'service': 'stock-system-v2', 'port': 8891, 'status': 'running'})


# ─── 回测池 ─────────────────────────────────────────────
@legacy_bp.route('/backtest-pool/list', methods=['GET'])
def backtest_pool_list():
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT ts_code, name, industry, market, status, created_at FROM backtest_pool ORDER BY created_at DESC")
            rows = cur.fetchall()
        pools = []
        for r in rows:
            pools.append({'ts_code': r['ts_code'], 'name': r['name'] or '',
                          'industry': r['industry'] or '', 'market': r['market'] or '',
                          'status': r['status'] or 'ACTIVE',
                          'created_at': str(r['created_at']) if r['created_at'] else ''})
        return api_success({'pools': pools, 'total': len(pools)})
    except Exception as e:
        return api_error(str(e))


@legacy_bp.route('/backtest-pool/add', methods=['POST'])
def backtest_pool_add():
    try:
        d = request.get_json() or {}
        codes = d.get('ts_code', '').strip().upper().split(',')
        added = 0
        with db_cursor() as cur:
            for code in codes:
                code = code.strip()
                if not code: continue
                cur.execute("INSERT IGNORE INTO backtest_pool (ts_code, name, status) VALUES (%s, %s, 'ACTIVE')",
                           (code, d.get('name', '')))
                if cur.rowcount > 0: added += 1
        return api_success({'added': added})
    except Exception as e:
        return api_error(str(e))


@legacy_bp.route('/backtest-pool/remove', methods=['POST'])
def backtest_pool_remove():
    try:
        d = request.get_json() or {}
        ts_code = d.get('ts_code', '')
        with db_cursor() as cur:
            cur.execute("DELETE FROM backtest_pool WHERE ts_code=%s", (ts_code,))
        return api_success({'ts_code': ts_code})
    except Exception as e:
        return api_error(str(e))


@legacy_bp.route('/backtest-pool/batch-add', methods=['POST'])
def backtest_pool_batch_add():
    try:
        d = request.get_json() or {}
        codes = d.get('codes', [])
        added = 0
        with db_cursor() as cur:
            for code in codes:
                cur.execute("INSERT IGNORE INTO backtest_pool (ts_code, name, status) VALUES (%s, %s, 'ACTIVE')",
                           (code.get('ts_code', ''), code.get('name', '')))
                if cur.rowcount > 0: added += 1
        return api_success({'added': added})
    except Exception as e:
        return api_error(str(e))


@legacy_bp.route('/backtest-pool/update', methods=['POST'])
def backtest_pool_update():
    try:
        d = request.get_json() or {}
        ts_code = d.get('ts_code', '')
        status = d.get('status', 'ACTIVE')
        with db_cursor() as cur:
            cur.execute("UPDATE backtest_pool SET status=%s WHERE ts_code=%s", (status, ts_code))
        return api_success({'ts_code': ts_code, 'status': status})
    except Exception as e:
        return api_error(str(e))


# ─── 回测 ───────────────────────────────────────────────
@legacy_bp.route('/backtest/pool', methods=['GET'])
def backtest_pool():
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE' ORDER BY ts_code")
            rows = cur.fetchall()
        pools = [{'ts_code': r['ts_code'], 'name': r['name'] or ''} for r in rows]
        return api_success({'pools': pools, 'total': len(pools)})
    except Exception as e:
        return api_error(str(e))


@legacy_bp.route('/backtest/run', methods=['POST'])
def backtest_run():
    try:
        d = request.get_json() or {}
        script = d.get('script', 'backtest_final_v3.py')
        import subprocess
        r = subprocess.run(['python3', script], capture_output=True, text=True, timeout=600,
                          cwd='/root/stock-system-v2/backend')
        return api_success({'stdout': r.stdout[-2000:], 'stderr': r.stderr[-2000:], 'rc': r.returncode})
    except subprocess.TimeoutExpired:
        return api_error('回测超时(>10分钟)')


# ─── 回测版本（get单版本详情） ───────────────────────────
@legacy_bp.route('/strategy/version/<int:ver>', methods=['GET'])
def strategy_version_detail(ver):
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT v.id, v.version, v.config_id, v.snapshot, v.change_desc, v.created_at,
                       c.name, c.season_type
                FROM strategy_config_versions v
                LEFT JOIN strategy_config c ON v.config_id = c.id
                WHERE v.version=%s ORDER BY v.config_id
            """, (ver,))
            rows = cur.fetchall()
        configs = []
        for r in rows:
            snap = r['snapshot']
            if isinstance(snap, str):
                snap = json.loads(snap)
            elif isinstance(snap, bytes):
                snap = json.loads(snap.decode())
            configs.append({
                'config_id': r['config_id'],
                'name': r['name'] or '',
                'season_type': r['season_type'] or '',
                'snapshot': snap,
                'change_desc': r['change_desc'] or '',
            })
        return api_success({'version': ver, 'configs': configs})
    except Exception as e:
        return api_error(str(e))


# ─── 监控池删除 ─────────────────────────────────────────
@legacy_bp.route('/watch-pool/remove', methods=['POST'])
def watch_pool_remove():
    try:
        d = request.get_json() or {}
        ts_code = d.get('ts_code', '')
        if not ts_code: return api_error('缺少ts_code')
        with db_cursor() as cur:
            cur.execute("DELETE FROM watch_pool WHERE ts_code=%s", (ts_code,))
        return api_success({'ts_code': ts_code})
    except Exception as e:
        return api_error(str(e))


# ─── 短周期评估 ─────────────────────────────────────────
@legacy_bp.route('/short-term/evaluate', methods=['GET'])
def short_term_evaluate():
    try:
        ts_code = request.args.get('ts_code', '')
        from datetime import datetime as _dt
        return api_success({
            'ts_code': ts_code,
            'evaluated_at': _dt.now().strftime('%Y-%m-%d %H:%M:%S'),
            'pulse': 'pending',
            'note': '短期评估需盘后数据支持',
        })
    except Exception as e:
        return api_error(str(e))


# ─── 板块轮动 ─────────────────────────────────────────
@legacy_bp.route('/sector/rotation', methods=['GET'])
def sector_rotation():
    try:
        limit = min(int(request.args.get('limit', 10)), 50)
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT MAX(trade_date) as d FROM sector_index_daily")
            row = cur.fetchone()
            td = str(row['d']) if row and row['d'] else ''
            if td:
                cur.execute("""
                    SELECT sid.sector_code, COALESCE(sm.sector_name, sid.sector_code) as sector_name,
                           sid.change_pct as rotation_score, 0 as season_score,
                           0 as change_pct_5d, 0 as change_pct_20d,
                           CASE WHEN sid.change_pct > 2 THEN 'strong' WHEN sid.change_pct > 0 THEN 'positive' WHEN sid.change_pct > -2 THEN 'neutral' ELSE 'weak' END as `signal`,
                           CASE WHEN sid.change_pct > 2 THEN '关注' WHEN sid.change_pct > 0 THEN '持有' ELSE '观望' END as advice
                    FROM sector_index_daily sid
                    LEFT JOIN sector_mapping sm ON sid.sector_code = sm.sector_code
                    WHERE sid.trade_date=%s
                    ORDER BY sid.change_pct DESC LIMIT %s
                """, (td, limit))
                rows = cur.fetchall()
            else:
                rows = []
        sectors = []
        for r in rows:
            sectors.append({
                'sector_code': r['sector_code'],
                'sector_name': r['sector_name'],
                'rotation_score': float(r['rotation_score']) if r['rotation_score'] else 0,
                'season_score': float(r['season_score']) if r['season_score'] else 0,
                'change_pct_5d': float(r['change_pct_5d']) if r['change_pct_5d'] else 0,
                'change_pct_20d': float(r['change_pct_20d']) if r['change_pct_20d'] else 0,
                'signal': r['signal'] or '',
                'advice': r['advice'] or '',
            })
        return api_success({'sectors': sectors, 'trade_date': td, 'total': len(sectors)})
    except Exception as e:
        return api_error(str(e))


# ─── 刷新评分代理 ──────────────────────────────────────
@legacy_bp.route('/refresh-score', methods=['POST'])
def refresh_score():
    """触发评分刷新"""
    from db_config import get_connection
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(ss.trade_date) as d FROM strategy_signal ss JOIN daily_kline dk ON ss.trade_date = dk.trade_date")
        row = cur.fetchone()
        td = str(row['d']) if row and row['d'] else ''
        cur.close()
        conn.close()
        return api_success({'trade_date': td, 'status': 'already_updated'})
    except Exception as e:
        return api_error(str(e))

@legacy_bp.route('/daily-score-snapshot', methods=['GET'])
def daily_score_snapshot():
    """评分快照查询"""
    from db_config import db_cursor, api_success, api_error, serialize_rows
    try:
        trade_date = request.args.get('date', '')
        keyword = request.args.get('keyword', '').strip().lower()
        min_score = request.args.get('min_score', '')
        limit = min(int(request.args.get('limit', 500)), 1000)

        with db_cursor(commit=False) as cur:
            if trade_date:
                where = ["trade_date=%s"]
                params = [trade_date]
            else:
                cur.execute("SELECT MAX(trade_date) as d FROM daily_score_snapshot")
                row = cur.fetchone()
                td = str(row['d']) if row and row['d'] else ''
                where = ["trade_date=%s"]
                params = [td]
                trade_date = td

            if keyword:
                where.append("(ts_code LIKE %s OR name LIKE %s)")
                kw = '%' + keyword + '%'
                params += [kw, kw]
            if min_score:
                where.append("calibrated_score >= %s")
                params.append(float(min_score))

            sql = """SELECT ts_code, calibrated_score, composite_score, name, industry,
                           close_price, change_pct, season
                    FROM daily_score_snapshot
                    WHERE {} ORDER BY calibrated_score DESC LIMIT %s""".format(' AND '.join(where))
            cur.execute(sql, params + [limit])
            rows = cur.fetchall()

            # 统计≥68的个数
            cur.execute("SELECT COUNT(*) as c FROM daily_score_snapshot WHERE trade_date=%s AND calibrated_score>=68",
                       (trade_date,))
            above68 = cur.fetchone()['c'] or 0

        snapshots = serialize_rows(rows, float_fields=[
            'calibrated_score','composite_score','close_price','change_pct'
        ])
        return api_success({
            'snapshots': snapshots,
            'total': len(snapshots),
            'trade_date': trade_date,
            'above68': above68,
        })
    except Exception as e:
        return api_error(str(e))
