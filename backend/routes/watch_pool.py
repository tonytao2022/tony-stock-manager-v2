"""
routes/watch_pool.py - 监控池管理路由
"""
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows
from auth import require_auth

watch_pool_bp = Blueprint('watch_pool', __name__)


@watch_pool_bp.route('/watch-pool/list', methods=['GET'])
@require_auth
def wp_list():
    """监控池列表（含最新评分）"""
    try:
        keyword = request.args.get('keyword', '').strip()
        page = int(request.args.get('page', 1))
        page_size = min(int(request.args.get('page_size', 50)), 200)
        offset = (page - 1) * page_size

        with db_cursor(commit=False) as cur:
            where = ["wp.is_active=1"]
            params = []
            if keyword:
                where.append("(wp.name LIKE %s OR wp.ts_code LIKE %s)")
                params.extend([f'%{keyword}%', f'%{keyword}%'])

            # 总数
            cur.execute(f"SELECT COUNT(*) FROM watch_pool wp WHERE {' AND '.join(where)}", params)
            total = cur.fetchone()['COUNT(*)']

            # 列表（含最新评分）
            cur.execute(f"""
                SELECT wp.*, ss.composite_score, ss.signal_type, ss.signal_label,
                       ss.trend_score, ss.momentum_score, ss.structure_score, ss.emotion_score,
                       ss.gate_triggered, ss.season, ss.trade_date
                FROM watch_pool wp
                LEFT JOIN strategy_signal ss ON wp.ts_code = ss.ts_code
                    AND ss.trade_date = (SELECT MAX(ss2.trade_date) FROM strategy_signal ss2 JOIN daily_kline dk ON ss2.trade_date = dk.trade_date)
                WHERE {' AND '.join(where)}
                ORDER BY wp.added_at DESC
                LIMIT %s OFFSET %s
            """, params + [page_size, offset])
            rows = cur.fetchall()

        return api_success({
            'stocks': serialize_rows(rows),
            'total': total,
            'page': page,
            'page_size': page_size,
        })
    except Exception as e:
        return api_error(str(e))


@watch_pool_bp.route('/watch-pool/add', methods=['POST'])
@require_auth
def wp_add():
    """添加监控股票"""
    try:
        data = request.get_json() or {}
        ts_code = data.get('ts_code', '').strip()
        name = data.get('name', '').strip()
        industry = data.get('industry', '')

        if not ts_code:
            return api_error('缺少ts_code')

        # 如果没有传name，从stock_basic查
        if not name:
            with db_cursor(commit=False) as cur:
                cur.execute("SELECT name, industry FROM stock_basic WHERE ts_code=%s LIMIT 1", [ts_code])
                row = cur.fetchone()
                if row:
                    name = row['name'] or ''
                    industry = industry or row.get('industry', '')

        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO watch_pool (ts_code, name, industry, reason, is_active)
                VALUES (%s, %s, %s, '手动添加', 1)
                ON DUPLICATE KEY UPDATE is_active=1, name=VALUES(name), industry=VALUES(industry)
            """, [ts_code, name, industry])

        return api_success({'ts_code': ts_code, 'name': name}, '添加成功')
    except Exception as e:
        return api_error(str(e))


@watch_pool_bp.route('/watch-pool/remove', methods=['POST'])
@require_auth
def wp_remove():
    """移除监控股票（软删除）"""
    try:
        data = request.get_json() or {}
        ts_code = data.get('ts_code', '').strip()

        if not ts_code:
            return api_error('缺少ts_code')

        with db_cursor() as cur:
            cur.execute("UPDATE watch_pool SET is_active=0 WHERE ts_code=%s", [ts_code])

        return api_success({'ts_code': ts_code}, '移除成功')
    except Exception as e:
        return api_error(str(e))


@watch_pool_bp.route('/watch-pool/batch-add', methods=['POST'])
@require_auth
def wp_batch_add():
    """批量添加（逗号分隔代码）"""
    try:
        data = request.get_json() or {}
        codes_str = data.get('codes', '')
        count = 0
        for code in codes_str.replace('，', ',').split(','):
            code = code.strip()
            if not code:
                continue
            with db_cursor(commit=False) as cur:
                cur.execute("SELECT name, industry FROM stock_basic WHERE ts_code=%s LIMIT 1", [code])
                row = cur.fetchone()
                name = row['name'] if row else code
                industry = row.get('industry', '') if row else ''
            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO watch_pool (ts_code, name, industry, reason, is_active)
                    VALUES (%s, %s, %s, '批量添加', 1)
                    ON DUPLICATE KEY UPDATE is_active=1
                """, [code, name, industry])
            count += 1
        return api_success({'added': count}, f'成功添加{count}只')
    except Exception as e:
        return api_error(str(e))

@watch_pool_bp.route('/watch-pool/score-list', methods=['GET'])
def watch_pool_score_list():
    """监控池评分列表（带实时价格和评分）"""
    from db_config import db_cursor, api_success, api_error, serialize_rows
    try:
        keyword = request.args.get('keyword', '')
        page = int(request.args.get('page', 1))
        page_size = min(int(request.args.get('page_size', 200)), 500)

        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT MAX(ss.trade_date) as d
                FROM strategy_signal ss
                JOIN daily_kline dk ON ss.trade_date = dk.trade_date
                GROUP BY ss.trade_date
                ORDER BY ss.trade_date DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            td = str(row['d']) if row and row['d'] else ''

            params = [td, td]
            where_extra = ''
            if keyword:
                kw = '%' + keyword + '%'
                where_extra = " AND (wp.ts_code LIKE %s OR COALESCE(wp.name, sb.name, '') LIKE %s)"
                params += [kw, kw]

            cur.execute(f"""
                SELECT wp.ts_code, COALESCE(wp.name, sb.name, '') as name,
                       ss.composite_score, ss.calibrated_score, ss.raw_score,
                       ss.trend_score, ss.momentum_score, ss.structure_score, ss.emotion_score,
                       ss.signal_label, ss.direction, ss.season, ss.pos_score, ss.mf_score,
                       ss.margin_score, ss.vol_ratio, ss.short_term_score,
                       ss.stf_capital, ss.stf_volume, ss.stf_overbought, ss.stf_momentum,
                       COALESCE(sb.industry, '') as industry,
                       dk.close as close_price, dk.change_pct,
                       ss.operation_mode, ss.is_filtered, ss.filter_reason
                FROM watch_pool wp
                LEFT JOIN stock_basic sb ON wp.ts_code = sb.ts_code
                LEFT JOIN strategy_signal ss ON wp.ts_code = ss.ts_code COLLATE utf8mb4_unicode_ci AND ss.trade_date = %s
                LEFT JOIN daily_kline_qfq dk ON wp.ts_code = dk.ts_code AND dk.trade_date = %s
                WHERE wp.is_active=1 {where_extra}
                ORDER BY ss.calibrated_score DESC, ss.composite_score DESC, wp.name ASC
            """, params)
            rows = cur.fetchall()

        stocks = serialize_rows(rows, float_fields=[
            'composite_score','calibrated_score','raw_score',
            'trend_score','momentum_score','structure_score','emotion_score',
            'pos_score','mf_score','margin_score','vol_ratio','close_price','change_pct',
            'short_term_score','stf_capital','stf_volume','stf_overbought','stf_momentum'
        ])
        return api_success({
            'stocks': stocks,
            'total': len(stocks),
            'trade_date': td,
        })
    except Exception as e:
        return api_error(str(e))
