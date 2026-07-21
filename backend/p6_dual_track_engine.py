#!/usr/bin/env python3
"""
P6 分季评分双轨引擎 v1.0
========================
2026-06-01 定案

设计者: Tony + Main + MAY

架构:
  season_engine.py → 季节+regime判定 (数出一源)
  ┣━ 轨道A: 动量评分 (夏季/春季/偏多混沌*)
  ┃   缠论趋势分×0.7 + 动量因子×0.3
  ┃   P3信号基於轨道排序
  ┗━ 轨道B: 均值回归评分 (秋季/冬季/混沌*)
       缠论结构×0.40 + 超跌深度×0.25 + ATR波动×0.10 + 资金因子×0.15 + 秋老虎+15分
       P3信号基於轨道排序

  V4: 动量权重从70/30改为50/25/25(缠论/动量/资金), 回归加入资金因子
  双轨排序: 动量轨道×1.3校准后合并排序 (Tony决策B)
  防跳变: 持有一天以上才能切换轨道 (Tony决策A)
  *混沌子态分配: 待MAY确认后补充
"""

import sys, os, math, json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")


# ⭐ penalty_log 持久化辅助函数
def _record_penalty(ts_code: str, trade_date, track: str, reasons: list, total_points: float, context: dict):
    """记录惩罚分明细到 penalty_log 表"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        now_val = str(trade_date) if not isinstance(trade_date, str) else trade_date

        # 解析 reasons 列表, 每条规则 INSERT 一笔
        for reason in reasons:
            # reason 格式: "破MA20(t80→48)" 或 "空头排列+5" 或 "5日跌-6%-15"
            for rule_name in ['破MA20', '空头排列', '5日跌', '10日跌', '20日跌']:
                if rule_name in reason:
                    # 提取分数
                    pts = 0.0
                    import re
                    pts_match = re.search(r'(\d+(\.\d+)?)$', reason.replace('-', ''))
                    if pts_match:
                        pts = float(pts_match.group(1))
                    
                    # 提取触发值
                    trigger_val = None
                    if rule_name == '破MA20':
                        trigger_val = round(float(context.get('ma20', 0)), 2)
                    elif rule_name == '空头排列':
                        trigger_val = round(float(context.get('close', 0)), 2)
                    elif rule_name == '5日跌':
                        trigger_val = round(float(context.get('r5', 0)) * 100, 2)
                    elif rule_name == '10日跌':
                        trigger_val = round(float(context.get('r10', 0)) * 100, 2)
                    elif rule_name == '20日跌':
                        trigger_val = round(float(context.get('r20', 0)) * 100, 2) if 'r20' in context else round(float(context.get('r20_ret', 0)) * 100, 2)
                    
                    cur.execute(
                        """INSERT INTO penalty_log
                           (ts_code, trade_date, track, rule_name, penalty_points, trigger_value)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (ts_code, now_val, track, rule_name, pts, trigger_val)
                    )
                    break
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        # penalty_log 是辅助信息, 不能因为写入失败影响主流程
        import logging
        logging.getLogger(__name__).warning(
            f'[penalty_log] 写入失败(ts_code={ts_code}, trade_date={now_val}): {e}')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from season_engine import SeasonEngine
from score_engine import score_chanlun_enhanced

# ============================================================
# 核心数据模型
# ============================================================

class MarketContext:
    """市场上下文——季节判定器输出的一次封装"""
    def __init__(self, judge_result: dict):
        self.season = judge_result.get('market_season', 'summer')
        self.regime = judge_result.get('market_regime', 'range')
        self.confidence = judge_result.get('market_confidence', 0.5)
        self.scoring_strategy = judge_result.get('market_scoring_strategy', 'momentum')
        self.trade_date = judge_result.get('trade_date', str(date.today()))
        self.raw = judge_result

    def is_momentum_track(self) -> bool:
        """
        是否走动量评分轨道
        
        P6定版 (MAY方案, 2026-06-01):
        动量轨道: 偏多混沌(任意regime) | 中性混沌+非熊市 | 春/夏
        回归轨道: 真秋/冬 | 偏空混沌 | 中性混沌+熊市
        """
        scoring = self.raw.get('scoring_strategy', 'momentum')
        return scoring == 'momentum'

    def momentum_multiplier(self) -> float:
        """
        动量轨道x1.3校准
        注意: 夏普高的秋季/混沌虽然走B轨,但B轨本身权重分配已不同
        """
        return 1.3 if self.is_momentum_track() else 1.0

    def get_hs300_trend(self) -> float:
        """获取沪深300近5日涨幅（从daily_kline读，qfq表数据不全）"""
        try:
            from db_config import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT close FROM daily_kline 
                WHERE ts_code='000300.SH' AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 5
            """, (self.trade_date,))
            rows = [float(r['close']) for r in cur.fetchall()]
            cur.close(); conn.close()
            if len(rows) >= 5:
                return (rows[0] - rows[-1]) / rows[-1]
            return 0.0
        except Exception as e:
            return 0.0

    def get_effective_season(self) -> str:
        """
        根据季节置信度加权校准 (P1-6, 2026-07-21)
        
        规则:
        - confidence >= 0.7: 原季节不变（标准买线）
        - confidence 0.4~0.7: 如果原季节是细分类型（如chaos_spring），回退到其父类（chaos）
        - confidence < 0.4: 强制回退到 chaos
        
        Returns:
            加权后的季节类型
        """
        conf = self.confidence
        season = self.season
        
        if conf >= 0.7:
            return season  # 高置信：维持原判
        elif conf >= 0.4:
            # 中置信：子态回退到父类
            parent_map = {
                'chaos_spring': 'chaos',
                'chaos_autumn': 'chaos',
                'weak_spring': 'spring',
                'weak_autumn': 'autumn',
            }
            return parent_map.get(season, season)
        else:
            # 低置信：强制回退混沌
            return 'chaos'

    def get_buy_line_override(self, original_buy_line: int, season: str) -> int:
        """
        基于置信度对买入线做微调
        
        confidence < 0.4 时买入线提高5分（更严格）
        """
        if self.confidence < 0.4 and season != 'chaos':
            return original_buy_line + 5  # 低置信度时更保守
        return original_buy_line


# ============================================================
# 轨道A: 动量评分
# ============================================================

def _calc_vol_ratio(ts_code: str, trade_date: str) -> float:
    """计算量比：当日vol / 前20日均vol（从daily_kline读，qfq数据不全）"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT k.vol / NULLIF(ma.avg_vol, 0) as vol_ratio
            FROM daily_kline k
            JOIN (
                SELECT AVG(vol) as avg_vol FROM daily_kline 
                WHERE ts_code=%s AND trade_date < %s AND trade_date >= DATE_SUB(%s, INTERVAL 20 DAY)
            ) ma ON 1=1
            WHERE k.ts_code=%s AND k.trade_date=%s
        """, (ts_code, trade_date, trade_date, ts_code, trade_date))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row['vol_ratio'] is not None:
            return float(row['vol_ratio'])
    except Exception as e:
        pass
    return 1.0


def _calc_moneyflow_score(ts_code: str, trade_date: str) -> tuple:
    """
    资金因子 v2.0 — 精细化重写

    优化点:
    1. 特大单净额独立评分（原仅用大单+特大单合计）
    2. 用net_mf_amount/流通市值标准化消除大小盘偏差
    3. 特大单/大单比值作为方向强度因子

    Returns:
        (moneyflow_score, net_mf_amount, smart_ratio)
    """
    try:
        conn = get_connection()
        cur = conn.cursor()

        # 1. 近5日累计净流入
        cur.execute("""
            SELECT
                COALESCE(SUM(m.net_mf_amount), 0) as mf_5d,
                COALESCE(SUM(m.buy_lg_amount - m.sell_lg_amount), 0) as lg_net_5d,
                COALESCE(SUM(m.buy_elg_amount - m.sell_elg_amount), 0) as elg_net_5d,
                COALESCE(SUM(m.buy_lg_amount + m.buy_elg_amount), 0) as buy_smart,
                COALESCE(SUM(m.sell_lg_amount + m.sell_elg_amount), 0) as sell_smart
            FROM moneyflow m
            WHERE m.ts_code=%s AND m.trade_date <= %s
              AND m.trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
        """, (ts_code, trade_date, trade_date))
        row = cur.fetchone()

        if not row or row['mf_5d'] is None:
            cur.close(); conn.close()
            return 50, 0, 0

        mf_5d = float(row['mf_5d'])
        lg_net = float(row['lg_net_5d'] or 0)
        elg_net = float(row['elg_net_5d'] or 0)
        buy_smart = float(row['buy_smart'] or 0)
        sell_smart = float(row['sell_smart'] or 0)

        # 2. 双分母标准化：净流入 / min(流通市值×1%, 日均成交额(估算)×2)
        cur.execute("""
            SELECT
                AVG(d.circ_mv) as avg_circ_mv,
                AVG(d.circ_mv * COALESCE(d.turnover_rate, 0.01)) as avg_est_amount
            FROM daily_basic d
            WHERE d.ts_code=%s AND d.trade_date <= %s
              AND d.trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
        """, (ts_code, trade_date, trade_date))
        mv_row = cur.fetchone()
        avg_circ_mv = float(mv_row['avg_circ_mv'] or 0) if mv_row else 0
        avg_est_amount = float(mv_row['avg_est_amount'] or 0) if mv_row else 0

        cur.close(); conn.close()

        # MAY建议：双分母 - 取市值1%和日均成交额2倍的较小值作为基准
        # 当circ_mv不可用(历史数据)时, fallback到原始净流入额评分(v1)
        if avg_circ_mv > 0 and avg_est_amount > 0:
            denom = min(avg_circ_mv * 0.01, avg_est_amount * 2.0)
            denom = max(denom, 10000)
            mf_ratio = mf_5d / denom if denom > 0 else 0
            use_double_denom = True
        else:
            # fallback: 直接按净流入绝对值评分
            mf_ratio = 0
            use_double_denom = False
        
        if use_double_denom:
            # 双分母标准化评分
            if mf_ratio > 0.03:     mf_score = 90
            elif mf_ratio > 0.015:  mf_score = 80
            elif mf_ratio > 0.005:  mf_score = 70
            elif mf_ratio > 0:      mf_score = 58
            elif mf_ratio > -0.005: mf_score = 42
            elif mf_ratio > -0.015: mf_score = 28
            elif mf_ratio > -0.03:  mf_score = 18
            else:                   mf_score = 10
        else:
            # fallback: 原始净流入额评分 (兼容历史数据)
            if mf_5d > 10000:       mf_score = 85
            elif mf_5d > 5000:      mf_score = 75
            elif mf_5d > 0:         mf_score = 60
            elif mf_5d > -5000:     mf_score = 40
            elif mf_5d > -10000:    mf_score = 25
            else:                   mf_score = 15

        # 基础评分（基于流通市值标准化比例）
        if mf_ratio > 0.03:
            mf_score = 90
        elif mf_ratio > 0.015:
            mf_score = 80
        elif mf_ratio > 0.005:
            mf_score = 70
        elif mf_ratio > 0:
            mf_score = 58
        elif mf_ratio > -0.005:
            mf_score = 42
        elif mf_ratio > -0.015:
            mf_score = 28
        elif mf_ratio > -0.03:
            mf_score = 18
        else:
            mf_score = 10

        # 特大单/大单比值（方向强度）
        total_smart = buy_smart + sell_smart + 1
        if total_smart > 1:
            elg_ratio = (abs(elg_net) + 1) / (abs(lg_net) + abs(elg_net) + 1)
            if elg_net > 0 and elg_ratio > 0.4:
                mf_score = min(100, mf_score + 12)
            elif elg_net > 0:
                mf_score = min(100, mf_score + 6)

            smart_ratio = (buy_smart - sell_smart) / total_smart
            if smart_ratio > 0.15:
                mf_score = min(100, mf_score + 8)
            elif smart_ratio < -0.15:
                mf_score = max(0, mf_score - 8)
        else:
            smart_ratio = 0

        # 近3日趋势一致性
        cur2 = get_connection()
        cur2c = cur2.cursor()
        cur2c.execute("""
            SELECT net_mf_amount FROM moneyflow
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 3
        """, (ts_code, trade_date))
        recent = cur2c.fetchall()
        cur2c.close(); cur2.close()

        if len(recent) >= 3:
            pos_days = sum(1 for r in recent if float(r['net_mf_amount'] or 0) > 0)
            if pos_days >= 2:
                mf_score = min(100, mf_score + 5)
            elif pos_days <= 1 and mf_5d < 0:
                mf_score = max(0, mf_score - 5)

        return round(mf_score, 1), round(mf_5d, 0), round(smart_ratio, 4)
    except Exception as e:
        import traceback; traceback.print_exc()
        return 50, 0, 0


def _calc_position_score(ts_code: str, trade_date_str: str) -> tuple:
    """
    计算位置因子：基于250日均线偏离度
    返回: (pos_score, pos_dev) 各0~100/浮点数
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT close, ma_250 FROM daily_kline d
            LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
            WHERE d.ts_code=%s AND d.trade_date <= %s
            ORDER BY d.trade_date DESC LIMIT 250
        """, (ts_code, trade_date_str))
        rows = cur.fetchall()
        if not rows or len(rows) < 250:
            return 50, 0.0
        
        last_close = float(rows[0]['close'])
        
        # 优先使用表里的ma250
        ma250 = float(rows[0].get('ma_250', 0) or 0)
        if ma250 <= 0:
            # 自己算
            ma250 = sum(float(r['close']) for r in rows[:250]) / 250
        
        dev = (last_close - ma250) / ma250 if ma250 > 0 else 0.0
        
        # 价格在均线附近60%仓位 -> 50分基准
        # 低于MA250超跌加分，高于MA250过热减分
        pos_score = (dev + 0.30) / 0.60 * 100
        pos_score = max(0, min(100, pos_score))
        return pos_score, round(dev, 4)
    except Exception as e:
        import traceback; traceback.print_exc()
        return 50, 0.0
    finally:
        cur.close(); conn.close()


def _calc_margin_score(ts_code: str, trade_date_str: str) -> float:
    """
    计算融资因子：基于近5日融资买入均值
    返回: 融资评分0~100
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT rzmre FROM margin_detail
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 5
        """, (ts_code, trade_date_str))
        rows = cur.fetchall()
        if not rows or len(rows) < 5:
            return 50
        rz_values = [float(r['rzmre'] or 0) for r in rows if r.get('rzmre') is not None]
        if not rz_values:
            return 50
        avg_rz = sum(rz_values) / len(rz_values)
        # log10(avg_rz万) -> 0~100 映射
        margin_score = math.log10(max(1, avg_rz / 10000)) / 5.0 * 100
        margin_score = max(0, min(100, margin_score))
        return margin_score
    except Exception as e:
        import traceback; traceback.print_exc()
        return 50
    finally:
        cur.close(); conn.close()


def track_momentum(ts_code: str, ctx: MarketContext) -> Dict:
    """
    动量轨道评分
    V13.3e (2026-07-20 MAY+Main联合调优) 权重:
    趋势×0.30 + 位置×0.08 + 结构×0.12 + 动量×0.30 + 资金×0.20 = 100%
    移除融资因子(僵尸因子,大部分票无数据退化为50分常量)
    结构分从5%提至12%, 动量从25%提至30%, 资金从15%提至20%
    
    Returns:
        {'track': 'momentum', 'score': float, 'details': {...}}
    """
    details = {'track': 'momentum'}
    
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # 获取最新K线
        cur.execute("""
            SELECT d.close, d.high, d.low, d.vol, d.amount, d.trade_date,
                   d.volume_ratio, d.turnover_rate,
                   t.ma_5, t.ma_10, t.ma_20, t.ma_60, t.ma_120, t.ma_250,
                   t.rsi_12 as rsi_14, t.macd_dif, t.macd_dea, t.atr_14,
                   t.boll_upper, t.boll_mid, t.boll_lower
            FROM daily_kline d
            LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
            WHERE d.ts_code=%s AND d.trade_date <= %s
            ORDER BY d.trade_date DESC LIMIT 120
        """, (ts_code, ctx.trade_date))
        rows = cur.fetchall()
        
        if not rows or len(rows) < 20:
            cur.close()
            return {'track': 'momentum', 'score': 50, 'reason': 'insufficient_data'}
        
        latest = rows[0]
        
        # 读取缠论结构评分
        cur.execute("""
            SELECT structure_score, buy_sell_point, beichi_type, beichi_strength,
                   zoushi_type, zoushi_stage, autumn_tiger, tiger_confidence
            FROM chanlun_structure 
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        cl_row = cur.fetchone()
        cur.close(); conn.close()
        
        # ──────── 计算六个因子 ────────
        
        # 1. 缠论趋势分 (30%)
        trend_score = 50
        if cl_row and cl_row.get('structure_score') is not None:
            ss = float(cl_row['structure_score'])
            if ss >= 75: trend_score = 85
            elif ss >= 60: trend_score = 70
            elif ss >= 40: trend_score = 55
            else: trend_score = 35
            
            bs = cl_row.get('buy_sell_point', 'none')
            bs_boost = {'buy3': 15, 'buy2': 8, 'buy1': 3, 'sell3': -15, 'sell2': -8, 'sell1': -3}.get(bs, 0)
            trend_score = max(0, min(100, trend_score + bs_boost))
            
            bt = cl_row.get('beichi_type', 'none')
            if bt == 'bottom' and float(cl_row.get('beichi_strength', 0) or 0) > 40:
                trend_score = min(100, trend_score + 10)
            elif bt == 'top' and float(cl_row.get('beichi_strength', 0) or 0) > 40:
                trend_score = max(0, trend_score - 10)
        else:
            close = float(latest['close'])
            ma20 = float(latest.get('ma20', 0) or 0)
            ma60 = float(latest.get('ma60', 0) or 0)
            if ma20 > 0 and ma60 > 0:
                if close > ma20 and ma20 > ma60: trend_score = 65
                elif close > ma20: trend_score = 55
                elif close > ma60: trend_score = 45
                else: trend_score = 35
        
        # 2. 缠论结构分 (5%) — 从chanlun_structure直接取
        structure_score = float(cl_row['structure_score']) if cl_row and cl_row.get('structure_score') is not None else 50
        structure_score = max(0, min(100, structure_score))
        
        # 3. 位置因子 (15%) — 基于250日均线偏离
        pos_score, pos_dev = _calc_position_score(ts_code, ctx.trade_date)
        
        # 4. 动量因子 (25%)
        closes = [float(r['close']) for r in reversed(rows)]
        n = len(closes)
        momentum = 50
        if n >= 20:
            r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
            r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0
            r20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0
            cons_up = 0
            for i in range(-5, 0):
                if closes[i] > closes[i-1]: cons_up += 1
                else: cons_up = 0
            rsi_val = float(latest.get('rsi_14', 50) or 50)
            
            score = 50
            score += max(-15, min(15, r5 * 150))
            score += max(-10, min(10, r10 * 80))
            score += max(-8, min(8, r20 * 50))
            score += min(8, cons_up * 2)
            score += (rsi_val - 50) * 0.5
            momentum = max(0, min(100, score))
        
        # 5. 资金因子 (15%)
        mf_score, mf_5d, lg_r = _calc_moneyflow_score(ts_code, ctx.trade_date)
        
        # 6. 融资因子 (10%)
        margin_score = _calc_margin_score(ts_code, ctx.trade_date)
        
        details['chanlun_trend'] = trend_score
        details['structure_score'] = structure_score
        details['pos_score'] = pos_score
        details['pos_dev'] = pos_dev
        details['momentum_raw'] = momentum
        details['mf_score'] = mf_score
        details['margin_score'] = margin_score
        details['mf_5d'] = round(mf_5d, 0)
        details['lg_ratio'] = round(lg_r, 4)
        details['chanlun_row'] = bool(cl_row)
        details['vol_ratio'] = _calc_vol_ratio(ts_code, ctx.trade_date)

        # ─── 价格下跌惩罚 V13.3b (2026-07-18, 回测最优版) ───
        # 对齐backtest_v133_fast.py的V13.3b规则：回测验证总收益+33.36%/卡玛3.20x
        penalty_score = 0.0
        penalty_reason = []

        if n >= 20:
            r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
            r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0
            r20_ret = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0

            close_price = float(latest['close'])
            ma5 = float(latest.get('ma_5', 0) or 0)
            ma20 = float(latest.get('ma_20', 0) or 0)

            # 如果technical_indicator没有MA数据，从closes自己算
            if ma20 <= 0 and len(closes) >= 20:
                ma20 = sum(closes[-20:]) / 20
            if ma5 <= 0 and len(closes) >= 5:
                ma5 = sum(closes[-5:]) / 5

            # ─── 破MA20 → 趋势分打折 ───
            if ma20 > 0 and close_price < ma20:
                below_ma20 = (ma20 - close_price) / ma20
                original_trend = trend_score
                discount = max(0.6, 1.0 - below_ma20 * 0.5)  # V13.3b: 打折更狠
                trend_score = int(original_trend * discount)
                if trend_score != original_trend:
                    tloss = (original_trend - trend_score) * 0.30
                    penalty_score += round(tloss, 1)
                    penalty_reason.append(f'破MA20(t{original_trend}→{trend_score})')
                    details['trend_orig'] = original_trend

            # ─── 空头排列检查（V13.3b: +8分）───
            if ma5 > 0 and ma20 > 0 and close_price < ma5 and ma5 < ma20:
                penalty_score += 5
                penalty_reason.append('空头排列+5')

            # ─── 跌幅惩罚 P1-5 (ATR阶梯收紧, 2026-07-21) ───
            # 阶梯式扣分：轻度亏损区(5-10%)不扣，中度(10-15%)轻扣，重度(15%+)中扣
            # 扣分上限：从每档15分调整为阶梯档10/15/20
            if r5 < -0.15:  # 5日跌超15% → 重罚
                p = min(20, int(abs(r5) * 120))
                penalty_score += p
                penalty_reason.append(f'5日跌{r5*100:.0f}%-重{p}')
            elif r5 < -0.10:  # 5日跌10-15% → 中罚
                p = min(15, int(abs(r5) * 140))
                penalty_score += p
                penalty_reason.append(f'5日跌{r5*100:.0f}%-中{p}')
            elif r5 < -0.05:  # 5日跌5-10% → 轻罚
                p = min(10, int(abs(r5) * 110))
                penalty_score += p
                penalty_reason.append(f'5日跌{r5*100:.0f}%-轻{p}')
            # -5%以内不扣

            if r10 < -0.20:  # 10日跌超20%
                p = min(20, int(abs(r10) * 100))
                penalty_score += p
                penalty_reason.append(f'10日跌{r10*100:.0f}%-重{p}')
            elif r10 < -0.12:  # 10日跌12-20%
                p = min(15, int(abs(r10) * 125))
                penalty_score += p
                penalty_reason.append(f'10日跌{r10*100:.0f}%-中{p}')
            elif r10 < -0.08:  # 10日跌8-12%
                p = min(10, int(abs(r10) * 100))
                penalty_score += p
                penalty_reason.append(f'10日跌{r10*100:.0f}%-轻{p}')
            # -8%以内不扣

            if r20_ret < -0.25:  # 20日跌超25%
                p = min(20, int(abs(r20_ret) * 80))
                penalty_score += p
                penalty_reason.append(f'20日跌{r20_ret*100:.0f}%-重{p}')
            elif r20_ret < -0.15:  # 20日跌15-25%
                p = min(15, int(abs(r20_ret) * 100))
                penalty_score += p
                penalty_reason.append(f'20日跌{r20_ret*100:.0f}%-中{p}')
            elif r20_ret < -0.10:  # 20日跌10-15%
                p = min(10, int(abs(r20_ret) * 100))
                penalty_score += p
                penalty_reason.append(f'20日跌{r20_ret*100:.0f}%-轻{p}')
            # -10%以内不扣

        details['penalty_score'] = round(penalty_score, 1)
        details['penalty_reason'] = ';'.join(penalty_reason) if penalty_reason else '无'

        # ⭐ penalty_log 持久化（每个惩罚规则一条记录）
        if penalty_score > 0:
            _record_penalty(ts_code, ctx.trade_date, 'momentum', penalty_reason, penalty_score,
                            {'r5':r5, 'r10':r10, 'r20_ret':r20_ret, 'close':close_price, 'ma5':ma5, 'ma20':ma20})

        # 7. 综合（扣除惩罚分）
        # V13.3e权重: 趋势30% + 位置8% + 结构12% + 动量30% + 资金20%
        final_score = (trend_score * 0.30 + pos_score * 0.08 + structure_score * 0.12 +
                       momentum * 0.30 + mf_score * 0.20)
        final_score = max(0, min(100, round(final_score - penalty_score, 1)))

        details['final_raw'] = round(final_score + penalty_score, 1)

        return {'track': 'momentum', 'score': final_score, 'details': details}

    except Exception as e:
        return {'track': 'momentum', 'score': 50, 'reason': str(e)}


# ============================================================
# 轨道B: 均值回归评分
# ============================================================

def track_reversion(ts_code: str, ctx: MarketContext) -> Dict:
    """
    均值回归轨道评分
    权重: 缠论结构×0.4 + 超跌深度×0.3 + ATR波动×0.2 + 秋老虎+15
    
    Returns:
        {'track': 'reversion', 'score': float, 'details': {...}}
    """
    details = {'track': 'reversion'}
    
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # 获取K线数据
        # 获取K线数据
        cur.execute("""
            SELECT d.close, d.high, d.low, d.vol, d.amount, d.trade_date,
                   d.volume_ratio, d.turnover_rate,
                   t.ma_5, t.ma_10, t.ma_20, t.ma_60, t.ma_120, t.ma_250,
                   t.rsi_12 as rsi_14, t.atr_14,
                   t.boll_upper, t.boll_mid, t.boll_lower
            FROM daily_kline d
            LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
            WHERE d.ts_code=%s AND d.trade_date <= %s
            ORDER BY d.trade_date DESC LIMIT 250
        """, (ts_code, ctx.trade_date))
        rows = cur.fetchall()
        
        if not rows or len(rows) < 60:
            cur.close()
            return {'track': 'reversion', 'score': 50, 'reason': 'insufficient_data'}
        
        latest = rows[0]
        close = float(latest['close'])
        
        # 读取缠论结构
        cur.execute("""
            SELECT structure_score, buy_sell_point, beichi_type, beichi_strength,
                   zoushi_type, zoushi_stage, autumn_tiger, tiger_confidence
            FROM chanlun_structure 
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        cl_row = cur.fetchone()
        
        # 读取趋势评分中的波动率
        cur.execute("""
            SELECT volatility_score
            FROM trend_score
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        trend_row = cur.fetchone()
        cur.close()
        
        # ===== 因子1: 缠论结构 (40%) =====
        structure = 50
        chanlun_exists = False
        if cl_row and cl_row.get('structure_score') is not None:
            chanlun_exists = True
            ss = float(cl_row['structure_score'])
            bs = cl_row.get('buy_sell_point', 'none')
            bt = cl_row.get('beichi_type', 'none')
            bstr = float(cl_row.get('beichi_strength', 0) or 0)
            at = bool(cl_row.get('autumn_tiger', 0))
            
            if ss >= 75: structure = 80
            elif ss >= 60: structure = 65
            elif ss >= 40: structure = 50
            else: structure = 35
            
            # 底背离=均值回归买点
            if bt == 'bottom' and bstr > 40:
                structure = min(100, structure + 15)
            elif bs in ('buy2', 'buy3'):
                structure = min(100, structure + 10)
            
            details['chanlun_structure'] = structure
            details['autumn_tiger'] = at
            
            # 秋老虎加分
            if at:
                structure = min(100, structure + 15)
        
        # ===== 因子2: 超跌深度 (30%) =====
        oversold = 50
        closes = [float(r['close']) for r in reversed(rows)]
        n = len(closes)
        
        if n >= 120:
            ma120 = float(latest.get('ma120', 0) or 0)
            ma250 = float(latest.get('ma250', 0) or 0)
            high_52w = float(latest.get('high_52w', 0) or 0)
            low_52w = float(latest.get('low_52w', 0) or 0)
            rsi_val = float(latest.get('rsi_14', 50) or 50)
            
            # 价格相对均线的偏离度
            if ma120 > 0:
                dev_ma120 = (close - ma120) / ma120
            else:
                dev_ma120 = 0
            
            if ma250 > 0:
                dev_ma250 = (close - ma250) / ma250
            else:
                dev_ma250 = 0
            
            # 位置区间: 52周高低
            if high_52w > low_52w:
                pos_52w = (close - low_52w) / (high_52w - low_52w)
            else:
                pos_52w = 0.5
            
            # 超跌评分:
            # 价格低于MA120=超跌特征, 越低越好
            # 价格在52周低位=超跌
            score = 50
            if dev_ma120 < -0.05: score += 5
            if dev_ma120 < -0.10: score += 8
            if dev_ma120 < -0.15: score += 5
            if dev_ma120 > 0.10: score -= 8  # 远离均线买入成本高
            
            if dev_ma250 < -0.05: score += 5
            if dev_ma250 < -0.15: score += 8
            
            # RSI极端: 超卖区加分
            if rsi_val < 25: score += 15
            elif rsi_val < 30: score += 10
            elif rsi_val < 40: score += 5
            elif rsi_val > 70: score -= 10
            elif rsi_val > 60: score -= 5
            
            # 52周低位
            if pos_52w < 0.20: score += 10
            elif pos_52w < 0.35: score += 5
            elif pos_52w > 0.80: score -= 8
            
            oversold = max(0, min(100, score))
            
            details['dev_ma120'] = round(dev_ma120, 3)
            details['dev_ma250'] = round(dev_ma250, 3)
            details['pos_52w'] = round(pos_52w, 3)
            details['rsi'] = rsi_val
        
        # ===== 因子3: ATR波动预警 (20%) =====
        volatility = 50
        if n >= 20:
            highs = [float(r['high']) for r in reversed(rows)]
            lows = [float(r['low']) for r in reversed(rows)]
            
            # 计算ATR
            tr_list = []
            for i in range(1, min(15, n)):
                tr = max(
                    highs[-i] - lows[-i],
                    abs(highs[-i] - closes[-i-1]),
                    abs(lows[-i] - closes[-i-1])
                )
                tr_list.append(tr)
            atr_val = sum(tr_list) / len(tr_list) if tr_list else 0
            atr_pct = atr_val / close if close > 0 else 0
            
            # 低波动=布局窗口, 高波动=警惕
            if atr_pct < 0.015:   # 极低波动
                volatility = 70
            elif atr_pct < 0.025: # 低波动
                volatility = 60
            elif atr_pct < 0.040: # 正常
                volatility = 50
            elif atr_pct < 0.060: # 高波动
                volatility = 35
            else:                 # 极高波动
                volatility = 20
            
            details['atr_pct'] = round(atr_pct, 4)
        
        # ===== 因子4: 资金因子 (15%) =====
        mf_score, mf_5d, lg_r = _calc_moneyflow_score(ts_code, ctx.trade_date)
        details['mf_score'] = mf_score
        details['mf_5d'] = round(mf_5d, 0)
        details['lg_ratio'] = round(lg_r, 4)
        details['vol_ratio'] = _calc_vol_ratio(ts_code, ctx.trade_date)
        
        # ===== 综合 =====
        # V4权重: 缠论×0.40 + 超跌×0.25 + ATR×0.10 + 资金×0.15 + 秋老虎+15
        final_score = structure * 0.40 + oversold * 0.25 + volatility * 0.10 + mf_score * 0.15
        
        # 秋老虎: 已经从structure中移除，单独加10分
        autumn_tiger = details.get('autumn_tiger', False)
        if autumn_tiger:
            final_score += 10
        
        final_score = max(0, min(100, round(final_score, 1)))
        
        details['structure_factor'] = structure
        details['oversold_factor'] = oversold
        details['volatility_factor'] = volatility
        details['mf_score'] = mf_score
        details['pos_score'] = oversold  # B轨超跌分当作位置因子复用
        details['structure_score'] = structure  # B轨结构分直接从缠论获取
        details['margin_score'] = 50     # B轨暂不计算融资因子，默认中性

        # ─── 价格下跌惩罚（同动量轨 V13.3b，回测最优版） ───
        penalty_score = 0.0
        penalty_reason = []
        if n >= 20:
            close_price = close
            # 使用已有的closes数组计算ma5、ma20和涨跌幅
            ma5_c = sum(closes[-5:]) / 5 if len(closes) >= 5 else 0
            ma20_c = sum(closes[-20:]) / 20
            r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
            r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0
            r20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0
            # 破MA20
            if ma20_c > 0 and close_price < ma20_c:
                below_ma20 = (ma20_c - close_price) / ma20_c
                discount = max(0.6, 1.0 - below_ma20 * 0.5)
                if discount < 1.0:
                    tloss = 55 * (1 - discount) * 0.40
                    if tloss > 2:
                        penalty_score += round(tloss, 1)
                        penalty_reason.append(f'破MA20-{round(tloss,1)}')
            # 空头排列
            if ma5_c > 0 and ma20_c > 0 and close_price < ma5_c and ma5_c < ma20_c:
                penalty_score += 5
                penalty_reason.append('空头排列+5')
            # 跌幅惩罚 P1-5 (ATR阶梯收紧, 同步动量轨)
            if r5 < -0.15:
                p = min(20, int(abs(r5) * 120))
                penalty_score += p
                penalty_reason.append(f'5日跌{r5*100:.0f}%-重{p}')
            elif r5 < -0.10:
                p = min(15, int(abs(r5) * 140))
                penalty_score += p
                penalty_reason.append(f'5日跌{r5*100:.0f}%-中{p}')
            elif r5 < -0.05:
                p = min(10, int(abs(r5) * 110))
                penalty_score += p
                penalty_reason.append(f'5日跌{r5*100:.0f}%-轻{p}')

            if r10 < -0.20:
                p = min(20, int(abs(r10) * 100))
                penalty_score += p
                penalty_reason.append(f'10日跌{r10*100:.0f}%-重{p}')
            elif r10 < -0.12:
                p = min(15, int(abs(r10) * 125))
                penalty_score += p
                penalty_reason.append(f'10日跌{r10*100:.0f}%-中{p}')
            elif r10 < -0.08:
                p = min(10, int(abs(r10) * 100))
                penalty_score += p
                penalty_reason.append(f'10日跌{r10*100:.0f}%-轻{p}')

            if r20 < -0.25:
                p = min(20, int(abs(r20) * 80))
                penalty_score += p
                penalty_reason.append(f'20日跌{r20*100:.0f}%-重{p}')
            elif r20 < -0.15:
                p = min(15, int(abs(r20) * 100))
                penalty_score += p
                penalty_reason.append(f'20日跌{r20*100:.0f}%-中{p}')
            elif r20 < -0.10:
                p = min(10, int(abs(r20) * 100))
                penalty_score += p
                penalty_reason.append(f'20日跌{r20*100:.0f}%-轻{p}')

        details['penalty_score'] = round(penalty_score, 1)
        details['penalty_reason'] = ';'.join(penalty_reason) if penalty_reason else '无'

        # ⭐ penalty_log 持久化
        if penalty_score > 0:
            _record_penalty(ts_code, ctx.trade_date, 'reversion', penalty_reason, penalty_score,
                            {'r5':r5, 'r10':r10, 'r20':r20, 'close':close_price, 'ma5':ma5_c, 'ma20':ma20_c})

        final_score = max(0, min(100, round(final_score - penalty_score, 1)))
        details['final_raw'] = round(final_score + penalty_score, 1)

        return {'track': 'reversion', 'score': final_score, 'details': details}
        
    except Exception as e:
        return {'track': 'reversion', 'score': 50, 'reason': str(e)}


# ============================================================
# V4过滤层：量比/资金/大盘强度
# ============================================================

def _apply_filters(results: List[Dict], trade_date: str, hs300_trend: float) -> Dict[str, str]:
    """
    对批量评分结果应用买入过滤层
    
    过滤规则:
    1. 爆量>2倍（拉高出货信号）→ 过滤  [P1-5:收紧至>100%量增]
    2. 大盘近5日跌>3% → 过滤（系统性风险）
    3. 资金近5日净流出+爆量 → 过滤（主力出逃）
    4. 缩量<0.5倍+资金流入 → 加分标记（地量见底）
    
    Returns:
        {ts_code: reason} 被过滤的原因
    """
    filter_reasons = {}
    
    for r in results:
        ts_code = r['ts_code']
        reasons = []
        
        # 计算量比（当日vol/前20日均量）
        vol_ratio = _calc_vol_ratio(ts_code, trade_date)
        
        # 规则1: 爆量>2倍（100%量增）→ 过滤 [P1-5: 收紧至2.0倍]
        if vol_ratio > 2.0:
            reasons.append(f'爆量{vol_ratio:.1f}倍>2')
        
        # 资金验证
        _, mf_5d, lg_r = _calc_moneyflow_score(ts_code, trade_date)
        
        # 规则3: 爆量+资金流出（主力出逃）
        if vol_ratio > 2.0 and mf_5d < -50000:
            reasons.append(f'爆量+资金流出{mf_5d/10000:.0f}万')
        
        # 规则4: 缩量<0.5倍+资金流入 → 加分（不过滤，只是标记）
        if vol_ratio < 0.5 and mf_5d > 0:
            r['_volume_bonus'] = True
        
        # 规则2: 大盘趋势判断（全局过滤）
        if hs300_trend < -0.03:
            r['_market_danger'] = True
            reasons.append(f'大盘跌{hs300_trend*100:.1f}%>3%')
        
        # 记录过滤状态
        if reasons:
            r['_filtered'] = True
            r['_filter_reasons'] = ';'.join(reasons)
            filter_reasons[ts_code] = ';'.join(reasons)
        else:
            r['_filtered'] = False
            r['_filter_reasons'] = ''
    
    return filter_reasons


# ============================================================
# 双轨评分主入口
# ============================================================

def _is_strong_stock(code, ctx):
    """
    判断个股是否为强势股（混沌期回退A轨用）
    标准：前20日涨幅 > 15%
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT close FROM daily_kline
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 20
        """, (code, ctx.trade_date))
        rows = [float(r['close']) for r in cur.fetchall()]
        cur.close(); conn.close()
        if len(rows) >= 20:
            ret = (rows[0] - rows[-1]) / rows[-1]
            return ret > 0.15
        return False
    except Exception as e:
        return False


def score_stock(ts_code: str, ctx: MarketContext) -> Dict:
    """
    双轨评分入口 + 独立短期过滤层
    
    V12.2 (MAY, 2026-06-16):
    混沌/秋/冬走B轨(均值回归)，但强势股(前20日涨幅>15%)回退到A轨(动量)

    V12.5 (2026-06-22) MAY建议：
    短期信号重构为独立过滤层 A/B档：
    - A档：短期分>=60 → 正常通过
    - B档：短期分<60 → 候选模式（评分不变，但前端标记为"候选"）
    不在engine内部改分，交给前端/策略层决策
    
    Returns:
        {'ts_code': ..., 'track': 'momentum'|'reversion', 
         'score': float, 'details': {...},
         'stf': {...}, 'stf_tier': 'A'|'B'}
    """
    if ctx.is_momentum_track():
        result = track_momentum(ts_code, ctx)
    else:
        if _is_strong_stock(ts_code, ctx):
            result = track_momentum(ts_code, ctx)
            result['track'] = 'momentum'
            result['_bailout'] = True
        else:
            result = track_reversion(ts_code, ctx)
    
    result['ts_code'] = ts_code
    
    # V12.5: 独立短期过滤层（MAY建议A/B档）
    try:
        from short_term_filter import calc_short_term_score
        stf = calc_short_term_score(ts_code, ctx.trade_date)
        result['stf'] = stf
        stfs = stf.get('short_term_score', 50)
        # MAY: 资金惯性权重应最大，这维单独检查
        cap_inertia = stf.get('capital_inertia', 50)
        
        # A/B档判定：短期分>=60为A档，<60为B档
        if stfs >= 60 and cap_inertia >= 50:
            result['stf_tier'] = 'A'
            result['_stf_tier_label'] = '😊 A档-优先'
        elif stfs >= 50:
            result['stf_tier'] = 'B'
            result['_stf_tier_label'] = '👀 B档-候选'
        else:
            result['stf_tier'] = 'B'
            result['_stf_tier_label'] = '👀 B档-候选'
            # 资金惯性太低(<35)的B档标为高风险
            if cap_inertia < 35:
                result['stf_tier'] = 'B_highrisk'
                result['_stf_tier_label'] = '⚠️ B档-高风险'
    except Exception as e:
        result['stf'] = {'short_term_score': 50, 'capital_inertia': 50}
        result['stf_tier'] = 'A'
        result['_stf_tier_label'] = '😊 默认'
    
    return result


def _build_calib_map(original_scores: List[float]) -> Dict[int, float]:
    """
    建立百分位映射校准表
    将P6原始分的排序位置映射到合理的校准分区间
    
    校准分目标分布（从v4历史分布验证）:
      P5=10, P10=15, P25=22, P50=30, P75=40, P90=50, P95=60, P100=80
    避免顶到100（丧失区分度）
    """
    n = len(original_scores)
    if n == 0: return {}
    sorted_scores = sorted(original_scores)
    targets = {
        5: 10, 10: 15, 15: 18, 20: 20, 25: 22, 30: 24,
        35: 26, 40: 28, 45: 29, 50: 30, 55: 32,
        60: 34, 65: 36, 70: 38, 75: 40, 80: 44,
        85: 48, 90: 50, 93: 55, 95: 60, 97: 68, 99: 75, 100: 80
    }
    calib_map = {}
    for pct, target in targets.items():
        idx = min(int(n * pct / 100), n - 1)
        raw = sorted_scores[idx]
        calib_map[raw] = target
    
    # 补全首尾
    if sorted_scores:
        calib_map[sorted_scores[0]] = max(0, targets.get(5, 10) - 5)
        calib_map[sorted_scores[-1]] = 80
    
    return calib_map


def _apply_calibration(raw_score: float, calib_map: Dict[int, float]) -> float:
    """
    对单个原始分应用百分位映射校准
    对映射表中每个断点做分段线性插值
    """
    if not calib_map:
        return max(0, min(100, raw_score))
    
    sorted_raws = sorted(calib_map.keys())
    
    # 边界处理
    if raw_score <= sorted_raws[0]:
        return float(calib_map[sorted_raws[0]])
    if raw_score >= sorted_raws[-1]:
        return float(calib_map[sorted_raws[-1]])
    
    # 分段线性插值
    for i in range(len(sorted_raws) - 1):
        lo_raw = sorted_raws[i]
        hi_raw = sorted_raws[i + 1]
        if lo_raw <= raw_score <= hi_raw:
            lo_cal = calib_map[lo_raw]
            hi_cal = calib_map[hi_raw]
            if hi_raw == lo_raw:
                return float(lo_cal)
            ratio = (raw_score - lo_raw) / (hi_raw - lo_raw)
            return round(lo_cal + ratio * (hi_cal - lo_cal), 1)
    
    return round(raw_score, 1)


def calibrate_scores(results: List[Dict]) -> List[Dict]:
    """
    对批量评分结果执行百分位映射校准
    
    两步:
    1. 根据所有原始分建立百分位映射表
    2. 对每个结果应用校准
    """
    raw_scores = [r['score'] for r in results if r.get('score') is not None]
    calib_map = _build_calib_map(raw_scores)
    
    for r in results:
        r['calibrated_score'] = _apply_calibration(r['score'], calib_map)
    
    results.sort(key=lambda x: x['calibrated_score'], reverse=True)
    return results


def batch_score(ts_codes: List[str], ctx: MarketContext) -> List[Dict]:
    """
    批量评分——全市场或监控池
    
    策略:
    1. 全部评分（不分轨道）
    2. 百分位映射校准（替代固定乘数×1.3）
    
    Returns:
        排序后的评分列表
    """
    results = []
    
    for ts_code in ts_codes:
        r = score_stock(ts_code, ctx)
        results.append(r)
    
    # 百分位映射校准（统一校准，不分轨道）
    calibrate_scores(results)
    
    return results


# ============================================================
# 防跳变 —— 轨道切换延迟一天
# ============================================================

class TrackHistory:
    """
    轨道切换防跳变
    原则: 持有一天以上才能切换轨道 (Tony决策A)
    """
    
    def __init__(self, db_table: str = 'strategy_signal'):
        self.db_table = db_table
        self._cache: Dict[str, Dict] = {}
    
    def get_previous_track(self, ts_code: str, date_str: str = None) -> Optional[str]:
        """获取上一个交易日的轨道"""
        from db_config import get_connection
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute(f"""
            SELECT track, trade_date FROM {self.db_table}
            WHERE ts_code=%s AND trade_date < %s
            ORDER BY trade_date DESC LIMIT 1
        """, (ts_code, date_str or date.today().isoformat()))
        row = cur.fetchone()
        cur.close()
        
        if row:
            return row['track']
        return None
    
    def should_switch(self, ts_code, new_track, current_date_str):
        """
        判定是否允许切换轨道
        
        规则:
        - 如果之前没有轨道记录 → 直接切换
        - 如果和之前相同 → 无需切换
        - 如果不同 → 检查切换间隔 > 1日 → 允许切换
        """
        prev = self.get_previous_track(ts_code, current_date_str)
        if prev is None:
            return True  # 首次,允许
        if prev == new_track:
            return True  # 相同轨道, 继续
        # 不同轨道: 需要间隔超过1天
        # 由batch_pipeline调度决定(只会每日计算一次)
        return False


# ============================================================
# 主管道
# ============================================================

def daily_pipeline(mode: str = 'watch_pool'):
    """
    每日评分管道
    
    Args:
        mode: 'watch_pool' | 'full_market'
    
    流程:
    1. SeasonEngine判断市场季节
    2. 获取评分池名单
    3. 双轨评分
    4. 校准+排序
    5. 入库
    """
    
    # 1. 市场季节判定 (数出一源)
    engine = SeasonEngine()
    judge_result = engine.judge_market_season()
    ctx = MarketContext(judge_result)
    
    print(f"📊 市场状态: {ctx.season}/{ctx.regime} | "
          f"策略: {ctx.scoring_strategy} | "
          f"轨道: {'动量' if ctx.is_momentum_track() else '均值回归'}")
    
    # 2. 获取评分池
    conn = get_connection()
    cur = conn.cursor()
    
    if mode == 'watch_pool':
        cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    else:
        cur.execute("SELECT DISTINCT ts_code FROM daily_kline WHERE trade_date=%s", 
                    (ctx.trade_date,))
    
    ts_codes = [row['ts_code'] for row in cur.fetchall()]
    tot = len(ts_codes)
    print(f"📈 评分池: {tot} 只股票")
    
    # 3. 批量评分
    results = batch_score(ts_codes, ctx)
    
    # 3.5 过滤层（V4: 基于量比/资金/大盘的买入过滤）
    hs300_trend = ctx.get_hs300_trend()
    print(f"📊 大盘强度(沪深300近5日): {hs300_trend*100:+.2f}%")
    filter_reasons = _apply_filters(results, ctx.trade_date, hs300_trend)
    filtered_out = [r['ts_code'] for r in results if r.get('_filtered', False)]
    print(f"🔒 过滤层: 排除{len(filtered_out)}只 | {len(results)-len(filtered_out)}只可通过")
    
    # 4. 入库+打印top
    saved, skipped = 0, 0
    for i, r in enumerate(results):
        try:
            code = r['ts_code']
            
            # 从 chanlun_structure 读取缠论买卖点（当日最新）
            cur.execute("""
                SELECT buy_sell_point, zoushi_type, beichi_type, structure_score,
                       autumn_tiger, tiger_confidence
                FROM chanlun_structure
                WHERE ts_code=%s AND trade_date=%s
                ORDER BY trade_date DESC LIMIT 1
            """, (code, ctx.trade_date))
            cl = cur.fetchone()
            
            bs = (cl['buy_sell_point'] or 'none') if cl else 'none'
            zt = (cl['zoushi_type'] or '未知') if cl else '未知'
            ss = float(cl['structure_score'] or 0) if cl else 0
            autumn = 1 if (cl and cl['autumn_tiger']) else 0
            tiger_conf = float(cl['tiger_confidence'] or 0) if cl else 0
            
            # 计算 operation_mode
            calib = float(r['calibrated_score'] or 0)
            if calib >= 75:
                op_mode = 'attack'
            elif calib >= 60:
                op_mode = 'normal'
            elif calib >= 40:
                op_mode = 'defense'
            else:
                op_mode = 'dormant'
            
            # 计算 signal_confidence
            if calib >= 80:
                sig_conf = 'high'
            elif calib >= 60:
                sig_conf = 'medium'
            else:
                sig_conf = 'low'
            
            # 构建 reason_chain
            track_label = '动量' if r['track'] == 'momentum' else '回归'
            reason_parts = [
                f"{ctx.season}+{ctx.regime}",
                f"{track_label}轨道",
            ]
            if bs and bs != 'none':
                reason_parts.append(f"{bs}确认")
            if zt and zt not in ('unknown', '未知'):
                reason_parts.append(zt)
            if ss >= 80:
                reason_parts.append('结构强势')
            elif ss >= 60:
                reason_parts.append('结构稳定')
            if autumn:
                reason_parts.append('秋老虎')
            reason = '+'.join(reason_parts)
            
            # V12.5: 短期信号分
            stf = r.get('stf', {}) or {}
            stf_score = float(stf.get('short_term_score', 50) or 50)
            stf_capital = float(stf.get('capital_inertia', 50) or 50)
            stf_volume = float(stf.get('volume_health', 50) or 50)
            stf_overbought = float(stf.get('overbought_safety', 50) or 50)
            stf_momentum = float(stf.get('short_momentum', 50) or 50)

            cur.execute("""
                # 从details取penalty_score（兼容轨道A/B两种详情结构）
                det = r.get('details', {}) or {}
                p_score = float(det.get('penalty_score', 0) or 0)
                p_reason = det.get('penalty_reason', '')

                INSERT INTO strategy_signal 
                    (ts_code, trade_date, track, composite_score, calibrated_score,
                     scoring_strategy, direction, operation_mode, buy_sell_point,
                     reason_chain, signal_confidence, autumn_tiger, tiger_confidence,
                     hengjiyuan_level, season,
                     penalty_score, penalty_reason,
                     short_term_score, stf_capital, stf_volume, stf_overbought, stf_momentum)
                VALUES (%s, %s, %s, %s, %s, %s, 'dual_track_v1', %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    track=VALUES(track), composite_score=VALUES(composite_score),
                    calibrated_score=VALUES(calibrated_score),
                    scoring_strategy=VALUES(scoring_strategy),
                    operation_mode=VALUES(operation_mode),
                    buy_sell_point=VALUES(buy_sell_point),
                    reason_chain=VALUES(reason_chain),
                    signal_confidence=VALUES(signal_confidence),
                    autumn_tiger=VALUES(autumn_tiger),
                    tiger_confidence=VALUES(tiger_confidence),
                    hengjiyuan_level=VALUES(hengjiyuan_level),
                    season=VALUES(season),
                    penalty_score=VALUES(penalty_score),
                    penalty_reason=VALUES(penalty_reason),
                    short_term_score=VALUES(short_term_score),
                    stf_capital=VALUES(stf_capital), stf_volume=VALUES(stf_volume),
                    stf_overbought=VALUES(stf_overbought), stf_momentum=VALUES(stf_momentum)
            """, (code, ctx.trade_date, r['track'],
                  r['score'], r['calibrated_score'],
                  'momentum' if r['track'] == 'momentum' else 'reversion',
                  op_mode, bs, reason, sig_conf,
                  autumn, tiger_conf,
                  ctx.raw.get('hengjiyuan_level', 'weak_heng'),
                  ctx.season,
                  p_score, p_reason,
                  stf_score, stf_capital, stf_volume, stf_overbought, stf_momentum))
            saved += 1
            if (i+1) % 10 == 0:
                conn.commit()
                print(f"  💾 已入库 {i+1}/{tot}")
        except Exception as e:
            skipped += 1
            if skipped <= 3:
                print(f"  ⚠️ 跳过 {r['ts_code']}: {e}")
    
    try:
        conn.commit()  # final flush
    except Exception as e:
        print(f"  ⚠️ 最终提交失败(可能已分批提交完成): {e}")
    try:
        cur.close()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    
    # 5. Top排序结果
    print(f"\n{'='*60}")
    print(f"🏆 P6 双轨评分 TOP 20 ({ctx.trade_date})")
    print(f"   市场: {ctx.season}/{ctx.regime} | 轨道: {'动量(A)' if ctx.is_momentum_track() else '回归(B)'}")
    print(f"{'='*60}")
    for i, r in enumerate(results[:20]):
        track_icon = '🚀' if r['track'] == 'momentum' else '🔄'
        print(f"{i+1:2d}. {track_icon} {r['ts_code']} | "
              f"分:{r['score']:5.1f} | 校准分:{r['calibrated_score']:5.1f} | "
              f"轨道:{r['track']}")
    
    print(f"\n📦 已入库: {saved} | 跳过: {skipped} | 评分池: {tot}")
    return results


# ============================================================
# 入口
# ============================================================

if __name__ == '__main__':
    results = daily_pipeline(mode='watch_pool')
