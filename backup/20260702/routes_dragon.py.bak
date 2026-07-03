"""
routes/dragon.py - 打板助手（涨停板/龙虎榜）
从v2_unified_api.py迁移完整逻辑：limit_up_daily + dragon_tiger_daily + 评分子项 + 打板评分
"""
import logging
from flask import Blueprint
from db_config import serialize_rows
from db_config import get_connection, api_success, api_error
from datetime import date, timedelta

logger = logging.getLogger('dragon_routes')
dragon_bp = Blueprint('dragon', __name__)


@dragon_bp.route('/dragon/list', methods=['GET'])
def dragon_list():
    """涨停板/龙虎榜列表（完整打板评分）"""
    try:
        conn = get_connection()
        cu = conn.cursor()

        # 取limit_up_daily最新交易日
        cu.execute("SELECT MAX(trade_date) as d FROM limit_up_daily")
        lt = cu.fetchone()['d']
        if not lt:
            return api_success({'data': [], 'trade_date': '', 'total': 0, 'strong': 0, 'buy': 0, 'watch': 0})

        # 取strategy_signal最新交易日（用于评分关联）
        cu.execute("SELECT MAX(trade_date) as d FROM strategy_signal WHERE trade_date >= %s", (lt,))
        st = cu.fetchone()['d'] or lt
        td = str(lt)
        std = str(st)

        # 涨停板数据（不含ST）
        cu.execute("""
            SELECT l.ts_code, l.name, l.trade_date, l.limit_up_time, 
                   l.open_times, l.sealed, l.change_pct, l.turnover_rate, l.reason
            FROM limit_up_daily l
            WHERE l.trade_date = %s
              AND l.name NOT LIKE CONCAT(CHAR(37), 'ST', CHAR(37))
              AND l.name NOT LIKE CONCAT(CHAR(37), '*ST', CHAR(37))
            ORDER BY l.limit_up_time
        """, (td,))
        rows = cu.fetchall()

        # 龙虎榜数据
        cu.execute("""
            SELECT ts_code, l_buy, l_sell, net_buy
            FROM dragon_tiger_daily
            WHERE trade_date = %s
            ORDER BY ABS(net_buy) DESC
        """, (td,))
        drm = {r['ts_code']: {'l_buy': float(r['l_buy'] or 0), 'l_sell': float(r['l_sell'] or 0), 'net_buy': float(r['net_buy'] or 0)} for r in cu.fetchall()}

        # 板块季节映射
        cu.execute("""
            SELECT index_code, season
            FROM season_state
            WHERE trade_date = (SELECT MAX(trade_date) FROM season_state WHERE index_code != 'MARKET')
              AND index_code != 'MARKET'
        """)
        isea = {r['index_code']: r['season'] for r in cu.fetchall()}
        cu.close()
        conn.close()

        res = []
        for r in rows:
            c2 = get_connection()
            c2c = c2.cursor()
            c2c.execute("""
                SELECT trend_score, momentum_score, pos_score, mf_score, 
                       margin_score, structure_score, calibrated_score, season
                FROM strategy_signal
                WHERE ts_code = %s AND trade_date = %s
                ORDER BY trade_date DESC LIMIT 1
            """, (r['ts_code'], std))
            s = c2c.fetchone()
            c2c.close()
            c2.close()

            tr = float(s['trend_score']) if s and s.get('trend_score') else 50
            mo = float(s['momentum_score']) if s and s.get('momentum_score') else 50
            po = float(s['pos_score']) if s and s.get('pos_score') else 50
            mf = float(s['mf_score']) if s and s.get('mf_score') else 50
            mg = float(s['margin_score']) if s and s.get('margin_score') else 50
            stv = float(s['structure_score']) if s and s.get('structure_score') else 50
            ca = float(s['calibrated_score']) if s and s.get('calibrated_score') else 0

            # 打板评分 = 动量35% + 趋势25% + 大单20% + 位置10% + 结构5% + 融资5%
            ds = mo * 0.35 + tr * 0.25 + mf * 0.20 + po * 0.10 + stv * 0.05 + mg * 0.05

            # 无策略评分时：涨停当天+15，早盘封板(9:25-10:00)+10
            if ca == 0:
                ds += 15
                lt_str = str(r.get('limit_up_time', '') or '')
                if lt_str and lt_str >= '09:25' and lt_str <= '10:00':
                    ds += 10

            # 板块季节
            tc = r['ts_code']
            if tc[:3] in ('688', '689'):
                ix = '000688.SH'
            elif tc.startswith('30'):
                ix = '399006.SZ'
            elif tc.endswith('.SH'):
                ix = '000001.SH'
            elif tc.endswith('.SZ'):
                ix = '399106.SZ'
            else:
                ix = '000300.SH'
            bs = isea.get(ix, 'chaos')
            ds += 5 if 'summer' in bs else (-5 if 'winter' in bs else 0)

            # 龙虎榜净额加分
            nb = drm.get(r['ts_code'], {}).get('net_buy', 0)
            if nb > 50000000:
                ds += 8
            elif nb > 10000000:
                ds += 3
            elif nb < -50000000:
                ds -= 8

            ds = max(0, min(100, round(ds, 1)))

            al = 'strong' if ds >= 80 else ('buy' if ds >= 70 else ('watch' if ds >= 60 else 'pass'))
            lab = '🔥 关注' if ds >= 80 else ('✅ 可打' if ds >= 70 else ('👀 观察' if ds >= 60 else '❌ 放弃'))

            res.append({
                'ts_code': r['ts_code'],
                'name': r.get('name', ''),
                'trade_date': td,
                'limit_time': str(r.get('limit_up_time') or '')[:5],
                'change_pct': float(r.get('change_pct') or 0),
                'reason': r.get('reason', '') or '',
                'dragon_score': ds,
                'action': lab,
                'action_level': al,
                'trend': round(tr, 1),
                'momentum': round(mo, 1),
                'pos_score': round(po, 1),
                'mf_score': round(mf, 1),
                'margin_score': round(mg, 1),
                'net_buy': round(nb, 0),
                'board_season': bs
            })

        res.sort(key=lambda x: x['dragon_score'], reverse=True)

        return api_success({
            'data': res,
            'trade_date': td,
            'total': len(res),
            'strong': len([x for x in res if x['action_level'] == 'strong']),
            'buy': len([x for x in res if x['action_level'] == 'buy']),
            'watch': len([x for x in res if x['action_level'] == 'watch'])
        })
    except Exception as e:
        import traceback
        return api_error(str(e) + '|' + traceback.format_exc()[:200])


@dragon_bp.route('/dragon/snapshot', methods=['GET'])
def dragon_snapshot():
    """打板评分快照查询（按日期+股票代码）"""
    try:
        from flask import request
        ts_code = request.args.get('ts_code', '')
        trade_date = request.args.get('trade_date', '')
        limit = min(int(request.args.get('limit', 50)), 500)

        from db_config import db_cursor
        with db_cursor(commit=False) as cur:
            sql = "SELECT * FROM dragon_snapshot WHERE 1=1"
            params = []
            if ts_code:
                sql += " AND ts_code LIKE %s"
                params.append('%' + ts_code + '%')
            if trade_date:
                sql += " AND trade_date = %s"
                params.append(trade_date)
            sql += " ORDER BY trade_date DESC, dragon_score DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
            rows = cur.fetchall()

        return api_success({
            'data': serialize_rows(rows, float_fields=['dragon_score','momentum','mf_score','trend','pos_score','margin_score','net_buy','change_pct']),
            'total': len(rows)
        })
    except Exception as e:
        return api_error(str(e))
