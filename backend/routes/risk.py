"""
routes/risk.py — 评分信号三级分级 + 持仓风险仪表盘
(纯价格/量/均线分析，无评分依赖)
"""
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows
import logging

risk_bp = Blueprint('risk', __name__)
logger = logging.getLogger(__name__)


def _get_api_key():
    """从db_config读取API key"""
    import os
    try:
        from db_config import db_cursor as _cur
        with _cur(commit=False) as cur:
            cur.execute("SELECT config_value FROM system_config WHERE config_key='api_key' LIMIT 1")
            row = cur.fetchone()
            if row:
                v = row['config_value'] if isinstance(row, dict) else row[0]
                return v.strip('"').strip("'")
    except:
        pass
    return os.environ.get('API_KEY', '')


def _is_market_open(trade_date_str: str) -> bool:
    """简单检查交易日是否有效"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT COUNT(*) as cnt FROM daily_kline WHERE trade_date=%s LIMIT 1
            """, (trade_date_str,))
            row = cur.fetchone()
            return (row and row['cnt'] > 0)
    except:
        return False


# ─── 信号分级工具函数 ───────────────────────────────────────

def _score_to_signal(score: float) -> dict:
    """原始评分 → 三级信号

    Returns: {signal_label, signal_emoji, action}
    """
    if score >= 70:
        return {"signal": "买入/持有", "emoji": "🟢", "action": "可以买入或持有"}
    elif score >= 50:
        return {"signal": "谨慎", "emoji": "🟡", "action": "进入观察期，不增仓"}
    elif score >= 30:
        return {"signal": "风险", "emoji": "⚠️", "action": "减仓至半仓或以下"}
    else:
        return {"signal": "清仓", "emoji": "🔴", "action": "建议全部退出"}


def _get_avg_volume(ts_code: str, trade_date: str, days: int = 20) -> float:
    """获取个股近期日均成交量（手）"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT AVG(vol) as avg_vol FROM daily_kline
                WHERE ts_code=%s AND trade_date < %s
                  AND trade_date >= DATE_SUB(%s, INTERVAL %s DAY)
            """, (ts_code, trade_date, trade_date, days))
            row = cur.fetchone()
            return float(row['avg_vol'] or 0)
    except:
        return 0


def _check_consecutive_drops(ts_code: str, trade_date: str, n: int = 3) -> bool:
    """检查是否连续N日下跌"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT change_pct FROM daily_kline
                WHERE ts_code=%s AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT %s
            """, (ts_code, trade_date, n))
            rows = cur.fetchall()
            if len(rows) < n:
                return False
            return all(float(r['change_pct'] or 0) < 0 for r in rows)
    except:
        return False


def _check_drawdown(ts_code: str, trade_date: str, threshold: float = -30.0) -> bool:
    """检查近期跌幅是否超过threshold%"""
    try:
        with db_cursor(commit=False) as cur:
            # 取最近21个交易日（约一个月）
            cur.execute("""
                SELECT low as min_price FROM daily_kline
                WHERE ts_code=%s AND trade_date <= %s
                  AND trade_date >= DATE_SUB(%s, INTERVAL 21 DAY)
                ORDER BY low ASC LIMIT 1
            """, (ts_code, trade_date, trade_date))
            low_row = cur.fetchone()
            if not low_row:
                return False
            min_close = float(low_row['min_price'])

            # 取当日close
            cur.execute("""
                SELECT close FROM daily_kline
                WHERE ts_code=%s AND trade_date=%s LIMIT 1
            """, (ts_code, trade_date))
            cur_row = cur.fetchone()
            if not cur_row:
                return False
            cur_price = float(cur_row['close'])

            # 取20日前close作为基准
            cur.execute("""
                SELECT close FROM daily_kline
                WHERE ts_code=%s AND trade_date <= DATE_SUB(%s, INTERVAL 20 DAY)
                ORDER BY trade_date DESC LIMIT 1
            """, (ts_code, trade_date))
            base_row = cur.fetchone()
            if not base_row or base_row['close'] == 0:
                return False
            base_price = float(base_row['close'])

            drop_pct = (cur_price - base_price) / base_price * 100
            return drop_pct <= threshold
    except:
        return False


def _get_last_n_days_change_pct(ts_code: str, trade_date: str, days: int = 5) -> float:
    """获取最近N天涨跌幅"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT close FROM daily_kline
                WHERE ts_code=%s AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 1
            """, (ts_code, trade_date))
            cur_row = cur.fetchone()
            if not cur_row:
                return 0

            cur.execute("""
                SELECT close FROM daily_kline
                WHERE ts_code=%s AND trade_date <= DATE_SUB(%s, INTERVAL %s DAY)
                ORDER BY trade_date DESC LIMIT 1
            """, (ts_code, trade_date, days - 1))
            prev_row = cur.fetchone()
            if not prev_row or float(prev_row['close'] or 0) == 0:
                return 0

            cur_p = float(cur_row['close'])
            prev_p = float(prev_row['close'])
            return (cur_p - prev_p) / prev_p * 100
    except:
        return 0


# ─── API: 评分信号三级分级 ──────────────────────────────────

@risk_bp.route('/api/v2/score-signal', methods=['GET'])
def score_signal():
    """返回所有评分记录转换后的三级信号分级

    Query params:
        date: 可选，指定交易日，默认最新
    """
    try:
        trade_date = request.args.get('date', '')

        with db_cursor(commit=False) as cur:
            # 获取最新交易日
            if not trade_date:
                cur.execute("""
                    SELECT MAX(ss.trade_date) as d
                    FROM strategy_signal ss
                    WHERE ss.trade_date IS NOT NULL
                """)
                row = cur.fetchone()
                trade_date = str(row['d']) if row and row['d'] else ''
                if not trade_date:
                    return api_error('无评分数据', http_status=404)

            # 获取当日所有评分数据
            cur.execute("""
                SELECT ss.ts_code,
                       COALESCE(sb.name, '') as name,
                       ss.composite_score, ss.calibrated_score,
                       ss.season, ss.trend_score, ss.momentum_score,
                       dk.close as close_price, dk.change_pct
                FROM strategy_signal ss
                LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                LEFT JOIN daily_kline dk ON ss.ts_code = dk.ts_code AND dk.trade_date = ss.trade_date
                WHERE ss.trade_date = %s
                ORDER BY ss.calibrated_score DESC
            """, (trade_date,))
            rows = cur.fetchall()

            if not rows:
                return api_error(f'{trade_date} 无评分数据', http_status=404)

            # 获取市场季节
            cur.execute("""
                SELECT season, raw_score FROM season_state
                WHERE index_code='MARKET' AND trade_date=%s LIMIT 1
            """, (trade_date,))
            season_row = cur.fetchone()

            season_row = cur.fetchone()

            # ── 批量获取趋势过滤数据 ──
            # 1) 连续3日下跌的股票
            consecutive_drop_codes = set()
            try:
                cur.execute("""
                    SELECT a.ts_code
                    FROM daily_kline a
                    JOIN daily_kline b ON a.ts_code = b.ts_code AND b.trade_date = DATE_SUB(a.trade_date, INTERVAL 1 DAY)
                    JOIN daily_kline c ON a.ts_code = c.ts_code AND c.trade_date = DATE_SUB(a.trade_date, INTERVAL 2 DAY)
                    WHERE a.trade_date = %s
                      AND a.change_pct < 0 AND b.change_pct < 0 AND c.change_pct < 0
                """, (trade_date,))
                consecutive_drop_codes = {r['ts_code'] for r in cur.fetchall()}
            except Exception as e:
                logger.warning('consecutive_drop query failed: %s', str(e)[:80])

            # 2) 已跌30%+的股票（底部保护）
            deep_drop_codes = set()
            try:
                cur.execute("""
                    SELECT a.ts_code
                    FROM daily_kline a
                    JOIN (
                        SELECT ts_code, MAX(trade_date) as max_date
                        FROM daily_kline
                        WHERE trade_date = %s
                        GROUP BY ts_code
                    ) latest ON a.ts_code = latest.ts_code AND a.trade_date = latest.max_date
                    WHERE (a.close / (
                        SELECT dk20.close FROM daily_kline dk20
                        WHERE dk20.ts_code = a.ts_code
                          AND dk20.trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
                          AND dk20.trade_date <= DATE_SUB(%s, INTERVAL 20 DAY)
                        ORDER BY dk20.trade_date DESC LIMIT 1
                    ) - 1) * 100 <= -30
                """, (trade_date, trade_date, trade_date))
                deep_drop_codes = {r['ts_code'] for r in cur.fetchall()}
            except Exception as e:
                logger.warning('deep_drop query failed: %s', str(e)[:80])

        market_season = season_row['season'] if season_row else 'chaos'
        raw_score = float(season_row['raw_score'] or 0) if season_row else 0

        # 计算平均分
        scores = [float(r['calibrated_score'] or r['composite_score'] or 0) for r in rows]
        avg_score = sum(scores) / len(scores) if scores else 0

        # 市场风险级别
        risk_level = 'defense'
        risk_label = '🔴 防御状态'
        if avg_score >= 70:
            risk_level = 'attack'
            risk_label = '🟢 进攻状态'
        elif avg_score >= 50:
            risk_level = 'normal'
            risk_label = '🟡 正常状态'

        # 转换每只股票信号（含趋势过滤）
        stocks = []
        for r in rows:
            ts_code = r['ts_code']
            score = float(r['calibrated_score'] or r['composite_score'] or 0)

            # 基础信号
            sig = _score_to_signal(score)
            signal = f"{sig['emoji']} {sig['signal']}"
            action = sig['action']

            # 趋势过滤：评分>70但股价连续3日下跌 → 降级为🟡
            if score >= 70 and ts_code in consecutive_drop_codes:
                signal = "🟡 谨慎"
                action = "进入观察期，不增仓（连续下跌降级）"

            # 底部保护：评分<30但已跌了30%+ → 升级为⚠️
            if score < 30 and ts_code in deep_drop_codes:
                signal = "⚠️ 风险"
                action = "减仓至半仓或以下（底部保护触发）"

            stocks.append({
                "code": ts_code,
                "name": r['name'] or '',
                "score": round(score, 1),
                "close_price": float(r['close_price'] or 0),
                "change_pct": float(r['change_pct'] or 0),
                "signal": signal,
                "action": action,
            })

        return api_success({
            "trade_date": trade_date,
            "risk_level": risk_level,
            "risk_label": risk_label,
            "avg_score": round(avg_score, 1),
            "market_season": market_season,
            "stocks": stocks,
            "total": len(stocks),
        })

    except Exception as e:
        logger.exception("score_signal API error")
        return api_error(str(e))


# ─── API: 持仓风险仪表盘 ────────────────────────────────────

@risk_bp.route('/api/v2/portfolio-risk', methods=['GET'])
def portfolio_risk():
    """持仓风险监控（纯价格/量/均线分析，无评分依赖）"""
    try:
        with db_cursor(commit=False) as cur:
            # 获取最新交易日
            cur.execute("SELECT MAX(trade_date) as d FROM daily_kline")
            row = cur.fetchone()
            trade_date = str(row['d']) if row and row['d'] else ''
            if not trade_date:
                return api_error('无K线数据', http_status=404)

            # 获取所有持仓
            cur.execute("""
                SELECT id, ts_code, name, cost_price, current_price, shares,
                       profit_pct, profit_amount, market_value, position_ratio, status
                FROM portfolio_holdings
                WHERE status IN ('hold', 'HOLDING', 'HOLD') AND shares > 0
                ORDER BY market_value DESC
            """)
            holdings = cur.fetchall()

            if not holdings:
                return api_success({
                    "trade_date": trade_date,
                    "stocks": [],
                    "total": 0,
                    "summary": {
                        "total_holdings": 0,
                        "high_risk_count": 0,
                        "panic_count": 0,
                    }
                })

            results = []
            high_risk_count = 0
            panic_count = 0

            for h in holdings:
                ts_code = h['ts_code']
                name = h['name']
                daily_change = 0.0
                weekly_change = 0.0
                ma20_deviation = 0.0
                volume_ratio = 1.0
                risk = "🟢"

                # 当日涨跌幅
                cur.execute("""
                    SELECT change_pct FROM daily_kline
                    WHERE ts_code=%s AND trade_date=%s LIMIT 1
                """, (ts_code, trade_date))
                dk_r = cur.fetchone()
                if dk_r:
                    daily_change = float(dk_r['change_pct'] or 0)
                else:
                    # 当日可能无数据，降级处理
                    continue

                # 5日涨跌幅
                weekly_change = _get_last_n_days_change_pct(ts_code, trade_date, 5)

                # MA20偏离（最近20日均线）
                cur.execute("""
                    SELECT AVG(close) as ma20 FROM daily_kline
                    WHERE ts_code=%s AND trade_date <= %s
                      AND trade_date > DATE_SUB(%s, INTERVAL 20 DAY)
                """, (ts_code, trade_date, trade_date))
                ma20_r = cur.fetchone()
                ma20 = float(ma20_r['ma20'] or 0) if ma20_r else 0

                # 当日收盘价
                cur.execute("""
                    SELECT close FROM daily_kline
                    WHERE ts_code=%s AND trade_date=%s LIMIT 1
                """, (ts_code, trade_date))
                close_r = cur.fetchone()
                close_price = float(close_r['close'] or 0) if close_r else 0

                if ma20 > 0 and close_price > 0:
                    ma20_deviation = (close_price - ma20) / ma20 * 100
                else:
                    ma20_deviation = 0

                # 量比（当日成交量 / 近20日均量）
                cur.execute("""
                    SELECT vol FROM daily_kline
                    WHERE ts_code=%s AND trade_date=%s LIMIT 1
                """, (ts_code, trade_date))
                vol_r = cur.fetchone()
                today_vol = float(vol_r['vol'] or 0) if vol_r else 0

                avg_vol = _get_avg_volume(ts_code, trade_date, 20)

                if avg_vol > 0:
                    volume_ratio = round(today_vol / avg_vol, 2)
                else:
                    volume_ratio = 1.0

                # ── 风险判定 ──
                risks = []

                # 规则1: 5日跌幅>25% → 🔴 紧急
                if weekly_change < -25:
                    risks.append("5日跌幅>25%")
                    risk = "🔴"

                # 规则2: MA20偏离>-20% → 🔴 严重
                if ma20_deviation < -20:
                    risks.append("MA20偏离>-20%")
                    risk = "🔴"

                # 规则3: 成交量近期日均2x + 当日跌超-5% → 🔴 恐慌出逃
                if volume_ratio >= 2.0 and daily_change < -5:
                    risks.append("恐慌出逃")
                    risk = "🔴"

                if risk == "🔴":
                    high_risk_count += 1
                    if "恐慌出逃" in risks:
                        panic_count += 1

                # 当前持仓盈亏
                profit_pct = float(h['profit_pct'] or 0)
                market_value = float(h['market_value'] or 0)
                cost_price = float(h['cost_price'] or 0)
                shares = float(h['shares'] or 0)
                current_price = float(h['current_price'] or 0)

                results.append({
                    "name": name,
                    "code": ts_code,
                    "daily_change": round(daily_change, 2),
                    "weekly_change": round(weekly_change, 2),
                    "ma20_deviation": round(ma20_deviation, 2),
                    "volume_ratio": volume_ratio,
                    "risk": risk,
                    "risk_reasons": "; ".join(risks) if risks else "正常",
                    "close_price": close_price,
                    "cost_price": cost_price,
                    "shares": shares,
                    "market_value": round(market_value, 2),
                    "profit_pct": round(profit_pct, 2),
                })

        return api_success({
            "trade_date": trade_date,
            "stocks": results,
            "total": len(results),
            "summary": {
                "total_holdings": len(results),
                "high_risk_count": high_risk_count,
                "panic_count": panic_count,
            }
        })

    except Exception as e:
        logger.exception("portfolio_risk API error")
        return api_error(str(e))
