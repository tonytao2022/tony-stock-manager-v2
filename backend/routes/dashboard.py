"""
routes/dashboard.py - 数据驾驶舱
（带内存缓存，auto-compact设计模式）
"""
import time
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows
from auth import require_auth
from tool_registry import register_tool

# ─── 内存缓存（参考Claude Code auto-compact设计） ────────────
_CACHE = {'data': None, 'time': 0}
_CACHE_TTL = 10  # 缓存10秒（80%请求不需要重新查库）


def _get_cached_or_refresh(fresh_func):
    """带TTL的内存缓存装饰器"""
    now = time.time()
    if _CACHE['data'] and now - _CACHE['time'] < _CACHE_TTL:
        return _CACHE['data']
    data = fresh_func()
    _CACHE['data'] = data
    _CACHE['time'] = now
    return data

dashboard_bp = Blueprint('dashboard', __name__)


@register_tool('dashboard', description='数据驾驶舱概览：市场状态+Top5评分+信号分布+持仓',
              is_readonly=True, is_concurrency_safe=True, cache_ttl=10)
@dashboard_bp.route('/dashboard', methods=['GET'])
def dashboard():
    """驾驶舱概览（带10秒缓存）"""
    return _get_cached_or_refresh(_fresh_dashboard)


def _fresh_dashboard():
    """真实查询（缓存未命中时调用）"""
    trade_date = request.args.get('date', '')

    with db_cursor(commit=False) as cur:
        # 获取最新评分日期
        if not trade_date:
            cur.execute("SELECT MAX(ss.trade_date) as d FROM strategy_signal ss JOIN daily_kline dk ON ss.trade_date = dk.trade_date")
            row = cur.fetchone()
            trade_date = str(row['d']) if row and row['d'] else ''

        # 市场状态
        cur.execute("""
            SELECT season, raw_score, confidence, position_advice,
                   hengjiyuan_level, hengjiyuan_score, confidence_mult
            FROM season_state
            WHERE index_code='MARKET'
            ORDER BY trade_date DESC LIMIT 1
        """)
        market = cur.fetchone()

        # 评分分布
        if trade_date:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN calibrated_score >= 75 THEN 1 ELSE 0 END) as strong_buy,
                    SUM(CASE WHEN calibrated_score >= 60 AND calibrated_score < 75 THEN 1 ELSE 0 END) as buy,
                    SUM(CASE WHEN calibrated_score >= 40 AND calibrated_score < 60 THEN 1 ELSE 0 END) as cautious,
                    SUM(CASE WHEN calibrated_score >= 20 AND calibrated_score < 40 THEN 1 ELSE 0 END) as hold,
                    SUM(CASE WHEN calibrated_score < 20 THEN 1 ELSE 0 END) as sell
                FROM strategy_signal
                WHERE trade_date=%s
            """, [trade_date])
            distribution = cur.fetchone()
        else:
            distribution = None

        # Top5 评分（仅从watch_pool中筛选）
        if trade_date:
            cur.execute("""
                SELECT ss.*, sb.industry, sb.name
                FROM strategy_signal ss
                LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                WHERE ss.trade_date=%s
                  AND ss.ts_code IN (SELECT ts_code FROM watch_pool WHERE is_active=1)
                ORDER BY ss.composite_score DESC LIMIT 5
            """, [trade_date])
            top5 = cur.fetchall()
        else:
            top5 = []

        # 持仓概览
        cur.execute("""
            SELECT COUNT(*) as total,
                   SUM(shares * current_price) as total_value,
                   SUM(profit_amount) as total_profit
            FROM portfolio_holdings
            WHERE status IN ('HOLDING', 'hold', 'locked')
        """)
        portfolio = cur.fetchone()
        # 持仓明细（前端需要holdings数组）
        cur.execute("""
            SELECT ts_code, name, shares, cost_price, current_price, market_value, profit_pct, profit_amount, status
            FROM portfolio_holdings
            WHERE status IN ('HOLDING', 'hold', 'locked') AND shares > 0
            ORDER BY market_value DESC
        """)
        holdings = cur.fetchall()

        market_data = {}
        if market:
            season = market.get('season', 'chaos') or 'chaos'
            emoji_map = {
                'spring': '🌺', 'summer': '☀️', 'autumn': '🍂', 'winter': '❄️',
                'chaos': '🌪️', 'chaos_spring': '🌤️', 'chaos_autumn': '🌥️',
            }
            market_data = {
                'season': season,
                'season_label': season,
                'season_emoji': emoji_map.get(season, '❓'),
                'raw_score': float(market.get('raw_score', 0) or 0),
                'confidence': float(market.get('confidence', 0) or 0),
                'position_advice': market.get('position_advice', ''),
                'hengjiyuan_level': market.get('hengjiyuan_level', ''),
                'hengjiyuan_score': float(market.get('hengjiyuan_score', 0) or 0),
                'confidence_mult': float(market.get('confidence_mult', 1) or 1),
            }

        dist_data = {'strong_buy': 0, 'buy': 0, 'cautious': 0, 'hold': 0, 'sell': 0}
        if trade_date:
            # 按个股季节的V12.2买入线统计信号分布
            # V12.2 买入线: summer=68, spring=65, autumn=72, winter=80, 混沌系=75
            cur.execute("""
                SELECT ss.calibrated_score, ss.season
                FROM strategy_signal ss
                WHERE ss.trade_date=%s AND ss.ts_code IN (SELECT ts_code FROM watch_pool WHERE is_active=1)
            """, (trade_date,))
            for r in cur.fetchall():
                ca = float(r['calibrated_score'] or 0)
                sea = r['season'] or 'chaos'
                # 按个股季节取买入线
                if sea in ('summer',): buy_line = 68
                elif sea in ('spring',): buy_line = 65
                elif sea in ('autumn',): buy_line = 72
                elif sea in ('winter',): buy_line = 80
                else: buy_line = 75  # chaos系
                
                if ca >= buy_line and ca >= 75:
                    dist_data['strong_buy'] += 1
                elif ca >= buy_line and ca >= 60:
                    dist_data['buy'] += 1
                elif ca >= 40:
                    dist_data['cautious'] += 1
                elif ca >= 20:
                    dist_data['hold'] += 1
                else:
                    dist_data['sell'] += 1

        return api_success({
            'trade_date': trade_date,
            'market': market_data,
            'distribution': dist_data,
            'top5': serialize_rows(top5),
            'portfolio': {
                'total_stocks': int(portfolio['total'] or 0),
                'total_value': float(portfolio['total_value'] or 0),
                'total_profit': float(portfolio['total_profit'] or 0),
                'holdings': serialize_rows(holdings, float_fields=[
                    'cost_price','current_price','market_value','profit_pct','profit_amount'
                ]),
            } if portfolio else {'total_stocks': 0, 'total_value': 0, 'total_profit': 0, 'holdings': []},
        })

@dashboard_bp.route('/intraday/signals', methods=['GET'])
def intraday_signals():
    """获取盘中实时信号"""
    from db_config import db_cursor, api_success, api_error, serialize_rows
    from datetime import datetime
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ts_code, trade_date, check_time, price, change_pct,
                       buy_sell_ratio, signal_label, signal_detail
                FROM intraday_signals
                WHERE trade_date = CURDATE()
                ORDER BY buy_sell_ratio ASC
            """)
            rows = cur.fetchall()
        # 将check_time从timedelta转成字符串
        signals = []
        for r in rows:
            s = dict(r)
            if isinstance(s.get('check_time'), (__import__('datetime').timedelta,)):
                td = s['check_time']
                h, m = divmod(td.seconds // 60, 60)
                s['check_time'] = f'{h:02d}:{m:02d}:{td.seconds%60:02d}'
            elif s.get('check_time'):
                s['check_time'] = str(s['check_time'])[:8]
            signals.append(s)
        return api_success({
            'signals': signals,
            'check_time': datetime.now().strftime('%H:%M:%S'),
        })
    except Exception as e:
        return api_error(str(e))

@dashboard_bp.route('/market-status', methods=['GET'])
def market_status():
    """市场状态 + 操作建议"""
    from db_config import db_cursor, api_success, api_error
    from datetime import date
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT MAX(trade_date) as d FROM strategy_signal WHERE trade_date <= CURDATE()")
            row = cur.fetchone()
            td = str(row['d']) if row and row['d'] else str(date.today())

            cur.execute("""
                SELECT season, regime, hengjiyuan_level, raw_score 
                FROM season_state WHERE index_code='MARKET' 
                ORDER BY trade_date DESC LIMIT 1
            """)
            sr = cur.fetchone()
            season = sr['season'] if sr else 'chaos'
            regime = sr['regime'] if sr else 'range'
            hengji = sr['hengjiyuan_level'] if sr else 'weak_heng'

            cur.execute("""
                SELECT COUNT(*) as total, AVG(composite_score) as avg_sc, 
                       AVG(calibrated_score) as avg_cal 
                FROM strategy_signal WHERE trade_date=%s
            """, (td,))
            sr2 = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM strategy_signal WHERE trade_date=%s AND composite_score>=75", (td,))
            above75 = cur.fetchone()['COUNT(*)']
            cur.execute("SELECT COUNT(*) FROM strategy_signal WHERE trade_date=%s AND composite_score>=60 AND composite_score<75", (td,))
            above60 = cur.fetchone()['COUNT(*)']

        return api_success({
            'trade_date': td,
            'season': season,
            'regime': regime,
            'hengji': hengji,
            'scoring_strategy': 'momentum_v2',
            'hs300_trend': 0,
            'total_stocks': int(sr2['total']) if sr2 else 0,
            'avg_score': round(float(sr2['avg_sc'] or 0), 1) if sr2 else 0,
            'avg_calibrated': round(float(sr2['avg_cal'] or 0), 1) if sr2 else 0,
            'above75': int(above75),
            'above60': int(above60),
        })
    except Exception as e:
        return api_error(str(e))


@dashboard_bp.route('/board-seasons', methods=['GET'])
def board_seasons():
    """各指数板块季节"""
    from db_config import db_cursor, api_success, api_error
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT index_code, season FROM season_state 
                WHERE trade_date = (SELECT MAX(trade_date) FROM season_state WHERE index_code!='MARKET')
                  AND index_code!='MARKET'
            """)
            seasons = {r['index_code']: r['season'] for r in cur.fetchall()}
        return api_success(seasons)
    except Exception as e:
        return api_error(str(e))
