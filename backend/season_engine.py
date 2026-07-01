#!/usr/bin/env python3
"""
恒纪元四季判定引擎 v2.1
=========================
v2.1 升级:
- 新增三态识别: 牛市(bull)/熊市(bear)/震荡(range) — RegimeDetector
- 动态季节阈值: 牛市中混沌区间更大，熊市中秋季更敏感
- 混沌细分: 偏多混沌/偏空混沌/中性混沌
- 评分策略切换: 输出 scoring_strategy (momentum=动量 / reversion=均值回归)

判定层面：大盘指数（沪深300/上证综指/创业板指/深证成指/科创50综合）
判定维度：6维度 >15项指标
输出：season + confidence + regime + scoring_strategy + rule_chain

设计者: May (首席模型设计师)
数据源: Tushare Pro → stock_db_v2.daily_kline
"""

import os
from db_config import get_connection, db_cursor, _get_password
import sys
import math
import json
import pymysql
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import warnings
warnings.filterwarnings("ignore")


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

INDEX_CONFIG = {
    '000300.SH': {'name': '沪深300', 'weight': 0.35, 'role': '主力资金'},
    '000001.SH': {'name': '上证综指', 'weight': 0.25, 'role': '全市场基准'},
    '399006.SZ': {'name': '创业板指', 'weight': 0.20, 'role': '成长活跃度'},
    '399001.SZ': {'name': '深证成指', 'weight': 0.15, 'role': '深圳综合'},
    '000688.SH': {'name': '科创50',   'weight': 0.05, 'role': '科技风向'},
}

# 季节分数映射
SEASON_MAP = {
    'spring': '🌸 春(进攻)',
    'summer': '☀️ 夏(持有)',
    'autumn': '🍂 秋(防守)',
    'winter': '❄️ 冬(休眠)',
    'chaos': '🌪️ 混沌(观望)',
    'chaos_spring': '🌤️ 弱春(偏多)',
    'chaos_autumn': '🌥️ 弱秋(偏空)',
}

# 维度权重
DIMENSION_WEIGHTS = {
    'ma_structure': 0.20,
    'momentum': 0.25,
    'volume_energy': 0.20,
    'volatility': 0.15,
    'market_breadth': 0.15,
    'trend_persistence': 0.05,
}




# ═══════════════════════════════════════════════════════════════
# 数据加载层
# ═══════════════════════════════════════════════════════════════

@dataclass
class KLineBar:
    """单日K线数据"""
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    change_pct: float
    vol: float
    amount: float

    @classmethod
    def from_row(cls, row: dict) -> 'KLineBar':
        return cls(
            trade_date=row['trade_date'] if isinstance(row['trade_date'], date)
            else row['trade_date'].date() if hasattr(row['trade_date'], 'date')
            else datetime.strptime(str(row['trade_date']), '%Y-%m-%d').date(),
            open=float(row['open']),
            high=float(row['high']),
            low=float(row['low']),
            close=float(row['close']),
            pre_close=float(row.get('pre_close', 0) or 0),
            change_pct=float(row.get('change_pct', 0) or 0),
            vol=float(row.get('vol', 0) or 0),
            amount=float(row.get('amount', 0) or 0),
        )


class DataLoader:
    """数据库数据加载器"""

    def __init__(self):
        self.conn = get_connection()

    def close(self):
        if self.conn:
            self.conn.close()

    def load_index_kline(self, ts_code: str, min_days: int = 200) -> List[KLineBar]:
        """加载指数日K线，确保足够天数用于计算MA120"""
        cur = self.conn.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            "SELECT * FROM daily_kline WHERE ts_code=%s AND is_valid=1 ORDER BY trade_date ASC",
            (ts_code,)
        )
        rows = cur.fetchall()
        cur.close()

        if len(rows) < min_days:
            return []

        return [KLineBar.from_row(r) for r in rows]

    def load_stock_kline_batch(self, trade_date: date, stock_codes: List[str]) -> Dict[str, dict]:
        """批量加载股票日K（用于市场宽度计算）"""
        if not stock_codes:
            return {}

        cur = self.conn.cursor(pymysql.cursors.DictCursor)
        # 查询当天的K线数据
        placeholders = ','.join(['%s'] * len(stock_codes))
        cur.execute(
            f"SELECT ts_code, trade_date, close FROM daily_kline "
            f"WHERE ts_code IN ({placeholders}) AND trade_date <= %s AND is_valid=1 "
            f"ORDER BY ts_code, trade_date DESC",
            (*stock_codes, trade_date)
        )
        rows = cur.fetchall()
        cur.close()

        # 去重取每个股票最新一条
        result = {}
        for r in rows:
            if r['ts_code'] not in result:
                result[r['ts_code']] = r
        return result

    def get_stock_pool_codes(self) -> List[str]:
        """获取回测池中的所有股票代码"""
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT ts_code FROM backtest_pool WHERE status='ACTIVE'")
        rows = cur.fetchall()
        cur.close()
        return [r[0] for r in rows]

    def load_stock_kline_history(self, ts_codes: List[str], lookback: int = 60) -> Dict[str, List[KLineBar]]:
        """批量加载个股历史K线"""
        if not ts_codes:
            return {}

        cur = self.conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ','.join(['%s'] * len(ts_codes))
        cur.execute(
            f"SELECT * FROM daily_kline WHERE ts_code IN ({placeholders}) "
            f"AND is_valid=1 ORDER BY ts_code, trade_date ASC",
            (*ts_codes,)
        )
        rows = cur.fetchall()
        cur.close()

        result: Dict[str, List[KLineBar]] = defaultdict(list)
        for r in rows:
            result[r['ts_code']].append(KLineBar.from_row(r))
        return dict(result)


# ═══════════════════════════════════════════════════════════════
# 技术指标工具箱
# ═══════════════════════════════════════════════════════════════

class TechIndicators:
    """技术指标计算（无外部依赖，纯数学实现）"""

    @staticmethod
    def sma(values: List[float], window: int) -> List[float]:
        """简单移动平均"""
        if len(values) < window:
            return [None] * len(values)
        result = [None] * len(values)
        for i in range(window - 1, len(values)):
            result[i] = sum(values[i - window + 1:i + 1]) / window
        return result

    @staticmethod
    def ema(values: List[float], window: int) -> List[float]:
        """指数移动平均"""
        if len(values) < 2:
            return [None] * len(values)
        k = 2.0 / (window + 1)
        result = [None] * len(values)
        result[0] = values[0]
        for i in range(1, len(values)):
            result[i] = values[i] * k + result[i - 1] * (1 - k)
        return result

    @staticmethod
    def roc(values: List[float], window: int) -> List[float]:
        """价格变化率 Rate of Change"""
        result = [None] * len(values)
        for i in range(window, len(values)):
            if values[i - window] != 0:
                result[i] = (values[i] - values[i - window]) / values[i - window]
        return result

    @staticmethod
    def atr(highs: List[float], lows: List[float], closes: List[float], window: int = 14) -> List[float]:
        """平均真实波幅"""
        n = len(closes)
        tr = [None] * n
        tr[0] = highs[0] - lows[0]
        for i in range(1, n):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
        return TechIndicators.sma(tr, window)

    @staticmethod
    def macd(closes: List[float],
             fast: int = 12, slow: int = 26, signal: int = 9
             ) -> Tuple[List[float], List[float], List[float]]:
        """MACD: 返回 (DIF, DEA, MACD柱)"""
        ema_fast = TechIndicators.ema(closes, fast)
        ema_slow = TechIndicators.ema(closes, slow)
        n = len(closes)
        dif = [None] * n
        for i in range(n):
            if ema_fast[i] is not None and ema_slow[i] is not None:
                dif[i] = ema_fast[i] - ema_slow[i]
        dea = TechIndicators.ema([d if d is not None else 0 for d in dif], signal)
        macd_hist = [None] * n
        for i in range(n):
            if dif[i] is not None and dea[i] is not None:
                macd_hist[i] = 2 * (dif[i] - dea[i])
        return dif, dea, macd_hist

    @staticmethod
    def rsi(closes: List[float], window: int = 14) -> List[float]:
        """相对强弱指标"""
        n = len(closes)
        gains = [0.0] * n
        losses = [0.0] * n
        for i in range(1, n):
            delta = closes[i] - closes[i - 1]
            if delta > 0:
                gains[i] = delta
            else:
                losses[i] = -delta

        avg_gain = TechIndicators.sma(gains, window)
        avg_loss = TechIndicators.sma(losses, window)

        rsi_values = [None] * n
        for i in range(window, n):
            if avg_loss[i] and avg_loss[i] > 0:
                rs = avg_gain[i] / avg_loss[i]
                rsi_values[i] = 100.0 - (100.0 / (1.0 + rs))
            elif avg_gain[i]:
                rsi_values[i] = 100.0
            else:
                rsi_values[i] = 0.0
        return rsi_values

    @staticmethod
    def bollinger_bands(closes: List[float], window: int = 20, num_std: float = 2.0
                        ) -> Tuple[List[float], List[float], List[float], List[float]]:
        """布林带: (中轨, 上轨, 下轨, 带宽)"""
        ma = TechIndicators.sma(closes, window)
        n = len(closes)
        upper = [None] * n
        lower = [None] * n
        bbw = [None] * n  # 带宽

        for i in range(window - 1, n):
            window_slice = closes[i - window + 1:i + 1]
            mean = sum(window_slice) / window
            variance = sum((x - mean) ** 2 for x in window_slice) / window
            std = math.sqrt(variance)
            upper[i] = ma[i] + num_std * std
            lower[i] = ma[i] - num_std * std
            if ma[i] and ma[i] > 0:
                bbw[i] = (upper[i] - lower[i]) / ma[i]

        return ma, upper, lower, bbw

    @staticmethod
    def consecutive_direction(values: List[float], idx: int) -> int:
        """计算从idx位置往前连续上行/下行的天数(正数=上行,负数=下行)"""
        if idx < 1:
            return 0
        direction = 1 if values[idx] > values[idx - 1] else -1
        count = 0
        i = idx
        while i > 0:
            cur_dir = 1 if values[i] > values[i - 1] else (-1 if values[i] < values[i - 1] else 0)
            if cur_dir == direction:
                count += 1
                i -= 1
            else:
                break
        return count * direction


# ═══════════════════════════════════════════════════════════════
# 三态识别器 (v2.1新增)
# ═══════════════════════════════════════════════════════════════

class RegimeDetector:
    """基于指数长期趋势判断牛市/熊市/震荡"""

    def __init__(self, bars: List[KLineBar]):
        self.closes = [b.close for b in bars]
        self.ma120 = TechIndicators.sma(self.closes, 120)
        self.ma60 = TechIndicators.sma(self.closes, 60)
        self.roc120 = TechIndicators.roc(self.closes, 120)
        self.roc60 = TechIndicators.roc(self.closes, 60)

    def detect_at(self, idx: int) -> Tuple[str, float, List[str]]:
        if idx < 120:
            return ('range', 0.0, ['数据不足'])

        details = []
        score = 0.0
        m120 = self.ma120[idx]
        m60 = self.ma60[idx]

        # MA120斜率(核心牛熊分界线)
        if m120 and idx >= 20 and self.ma120[idx - 20]:
            m120_slope = (m120 - self.ma120[idx - 20]) / self.ma120[idx - 20]
            if m120_slope > 0.05:
                score += 5
                details.append(f'MA120陡升(+{m120_slope:.1%})')
            elif m120_slope > 0.02:
                score += 3
                details.append(f'MA120上升(+{m120_slope:.1%})')
            elif m120_slope < -0.05:
                score -= 5
                details.append(f'MA120陡降({m120_slope:.1%})')
            elif m120_slope < -0.02:
                score -= 3
                details.append(f'MA120下降({m120_slope:.1%})')

        # 120日ROC
        if self.roc120[idx] is not None:
            r120 = self.roc120[idx]
            if r120 > 0.20:
                score += 4
                details.append(f'120日涨幅+{r120:.0%}(强牛)')
            elif r120 > 0.08:
                score += 2
                details.append(f'120日涨幅+{r120:.0%}')
            elif r120 < -0.15:
                score -= 4
                details.append(f'120日跌幅{r120:.0%}(熊市)')
            elif r120 < -0.08:
                score -= 2
                details.append(f'120日跌幅{r120:.0%}')

        # 价格vs MA120
        if m120:
            pct = self.closes[idx] / m120 - 1
            if pct > 0.10:
                score += 2
                details.append(f'>MA120({pct:+.1%})')
            elif pct < -0.10:
                score -= 3
                details.append(f'<MA120({pct:.1%})')

        # MA60 vs MA120
        if m60 and m120:
            if m60 > m120 * 1.03:
                score += 2
                details.append('MA60>>MA120')
            elif m60 < m120 * 0.97:
                score -= 2
                details.append('MA60<<MA120')

        # 近60日最大回撤
        mdd = self._max_drawdown(idx, 60)
        if mdd > 0.15:
            score -= 3
            details.append(f'60日最大回撤{mdd:.0%}')
        elif mdd > 0.08:
            score -= 1

        if score >= 4:
            regime = 'bull'
            strength = min(1.0, score / 10)
        elif score <= -4:
            regime = 'bear'
            strength = min(1.0, abs(score) / 10)
        else:
            regime = 'range'
            strength = 1.0 - abs(score) / 4

        return regime, strength, details

    def _max_drawdown(self, idx, window):
        best = 0
        mdd = 0.0
        for i in range(max(0, idx - window), idx + 1):
            if self.closes[i] > best:
                best = self.closes[i]
            if best > 0:
                mdd = min(mdd, (self.closes[i] - best) / best)
        return abs(mdd)


# ═══════════════════════════════════════════════════════════════
# 六维度因子打分引擎 (v2.1 — 三态识别+动态阈值+混沌细分)
# ═══════════════════════════════════════════════════════════════

REGIME_MAP = {'bull': '🐂 牛市', 'bear': '🐻 熊市', 'range': '📊 震荡'}

@dataclass
class DimensionResult:
    """单个维度的打分结果"""
    name: str
    score: float  # -10 to +10
    weight: float
    details: List[str] = field(default_factory=list)


class SeasonJudge:
    """四季判定核心逻辑"""

    def __init__(self, bars: List[KLineBar]):
        """
        Args:
            bars: 指数日K线，按日期升序排列，需 >=130 条
        """
        if len(bars) < 130:
            raise ValueError(f"数据不足: {len(bars)}条, 需要>=130条")

        self.bars = bars
        self.closes = [b.close for b in bars]
        self.highs = [b.high for b in bars]
        self.lows = [b.low for b in bars]
        self.vols = [b.vol for b in bars]
        self.amounts = [b.amount for b in bars]
        self.dates = [b.trade_date for b in bars]
        self.n = len(bars)

        # 预计算所有技术指标
        self.ma20 = TechIndicators.sma(self.closes, 20)
        self.ma60 = TechIndicators.sma(self.closes, 60)
        self.ma120 = TechIndicators.sma(self.closes, 120)
        self._ema12 = TechIndicators.ema(self.closes, 12)
        self._ema26 = TechIndicators.ema(self.closes, 26)
        self.roc5 = TechIndicators.roc(self.closes, 5)
        self.roc20 = TechIndicators.roc(self.closes, 20)
        self.atr14 = TechIndicators.atr(self.highs, self.lows, self.closes, 14)
        self._vol_ma20 = TechIndicators.sma(self.vols, 20)
        self.rsi14 = TechIndicators.rsi(self.closes, 14)
        self._bb_ma, self._bb_upper, self._bb_lower, self._bb_width = \
            TechIndicators.bollinger_bands(self.closes, 20, 2.0)
        self._dif, self._dea, self._macd_hist = TechIndicators.macd(self.closes)

        # v2.1: 三态识别器
        self.regime_detector = RegimeDetector(bars)

    def judge_at(self, idx: int, market_breadth_mapped: Optional[Dict] = None) -> Tuple[str, float, Dict]:
        """
        在指定索引位置进行四季判定

        Args:
            idx: bars中的位置索引
            market_breadth_mapped: 市场宽度预计算结果(可选)，含:
                - pct_above_ma20: MA20之上的股票占比
                - new_high_low_ratio: 20日新高/新低比

        Returns:
            (season, confidence, dimensions_detail)
        """
        dimensions: List[DimensionResult] = []

        # ─── 维度1: 均线结构 ───
        dim1 = self._judge_ma_structure(idx)
        dimensions.append(dim1)

        # ─── 维度2: 动量强度 ───
        dim2 = self._judge_momentum(idx)
        dimensions.append(dim2)

        # ─── 维度3: 成交量能量 ───
        dim3 = self._judge_volume_energy(idx)
        dimensions.append(dim3)

        # ─── 维度4: 波动率环境 ───
        dim4 = self._judge_volatility(idx)
        dimensions.append(dim4)

        # ─── 维度5: 市场宽度 ───
        dim5 = self._judge_market_breadth(idx, market_breadth_mapped)
        dimensions.append(dim5)

        # ─── 维度6: 趋势持续性 ───
        dim6 = self._judge_trend_persistence(idx)
        dimensions.append(dim6)

        # ─── 加权汇总 ───
        raw_score = sum(d.score * d.weight for d in dimensions)

        # v2.1: 三态识别
        regime, regime_strength, regime_reasons = self.regime_detector.detect_at(idx)

        # v2.1: 动态阈值季节映射
        season = self._score_to_season_adjusted(raw_score, regime)

        # 混沌细分
        chaos_subtype = None
        if season == 'chaos':
            if raw_score > 1.0:
                chaos_subtype = 'chaos_bullish'
            elif raw_score < -1.0:
                chaos_subtype = 'chaos_bearish'
            else:
                chaos_subtype = 'chaos_neutral'

        # 评分策略
        scoring_strategy = self._get_scoring_strategy(season, regime, chaos_subtype)

        # 置信度: 维度评分的一致性
        scores = [d.score for d in dimensions]
        confidence = self._calc_confidence(scores)

        # 构建详细输出
        details = {
            'ts_code': 'INDEX',
            'trade_date': self.dates[idx].isoformat() if hasattr(self.dates[idx], 'isoformat') else str(self.dates[idx]),
            'raw_score': round(raw_score, 2),
            'season': season,
            'confidence': round(confidence, 3),
            'regime': regime,
            'regime_strength': round(regime_strength, 3),
            'regime_reasons': regime_reasons,
            'chaos_subtype': chaos_subtype,
            'scoring_strategy': scoring_strategy,
            'dimensions': {
                d.name: {
                    'score': round(d.score, 2),
                    'weight': d.weight,
                    'details': d.details,
                }
                for d in dimensions
            },
            'rule_chain': self._build_rule_chain(dimensions, season, raw_score, confidence, regime, chaos_subtype, scoring_strategy),
        }

        return season, confidence, details

    # ─── 维度1: 均线结构 (权重20%) ───
    def _judge_ma_structure(self, idx: int) -> DimensionResult:
        details = []
        score = 0.0

        m20, m60, m120 = self.ma20[idx], self.ma60[idx], self.ma120[idx]
        close = self.closes[idx]

        if m20 is None or m60 is None or m120 is None:
            return DimensionResult('ma_structure', 0, 0.20, ['MA数据不足'])

        # 均线方向（用5日前的MA做对比，避免噪音）
        m20_dir = self._slope_sign(m20, self._safe_ma(self.ma20, idx, -5))
        m60_dir = self._slope_sign(m60, self._safe_ma(self.ma60, idx, -5))
        m120_dir = self._slope_sign(m120, self._safe_ma(self.ma120, idx, -5))

        dir_sum = m20_dir + m60_dir + m120_dir  # -3 to +3

        if dir_sum >= 2:
            score += 5
            details.append(f'三线多头排列(方向分={dir_sum})')
        elif dir_sum <= -2:
            score -= 5
            details.append(f'三线空头排列(方向分={dir_sum})')
        else:
            details.append(f'均线方向分歧(方向分={dir_sum})')

        # 发散度
        if m120 > 0:
            spread = abs(m20 - m120) / m120
            if spread > 0.12:
                score += 3 if dir_sum > 0 else -2
                details.append(f'均线强发散(spread={spread:.1%})')
            elif spread < 0.03:
                score -= 1
                details.append(f'均线缠绕(spread={spread:.1%})')

        # 价格位置
        if m20 and close:
            pct_ma20 = (close - m20) / m20
            if pct_ma20 > 0.03 and dir_sum > 0:
                score += 2
                details.append(f'价格强势站上MA20(+{pct_ma20:.1%})')
            elif pct_ma20 < -0.03 and dir_sum < 0:
                score -= 2
                details.append(f'价格深度跌破MA20({pct_ma20:.1%})')

        return DimensionResult('ma_structure', max(-10, min(10, score)), 0.20, details)

    # ─── 维度2: 动量强度 (权重25%) ───
    def _judge_momentum(self, idx: int) -> DimensionResult:
        details = []
        score = 0.0

        roc20_val = self.roc20[idx]
        roc5_val = self.roc5[idx]

        # ROC(20) 打分
        if roc20_val is not None:
            if roc20_val > 0.08:
                score += 7
                details.append(f'ROC20强劲(+{roc20_val:.1%})')
            elif roc20_val > 0.03:
                score += 4
                details.append(f'ROC20正常偏多(+{roc20_val:.1%})')
            elif roc20_val > -0.02:
                score += 1
                details.append(f'ROC20横盘({roc20_val:.1%})')
            elif roc20_val > -0.05:
                score -= 2
                details.append(f'ROC20偏弱({roc20_val:.1%})')
            elif roc20_val > -0.10:
                score -= 5
                details.append(f'ROC20弱势({roc20_val:.1%})')
            else:
                score -= 7
                details.append(f'ROC20极弱({roc20_val:.1%})')

        # 动量加速度: ROC5 vs ROC20
        if roc5_val is not None and roc20_val is not None:
            accel = roc5_val - roc20_val
            if accel > 0.03:
                score += 3
                details.append(f'动量加速上行(accel={accel:+.1%})')
            elif accel < -0.03:
                score -= 3
                details.append(f'动量加速下行(accel={accel:+.1%})')

        # 连续方向天数
        cons_dir = TechIndicators.consecutive_direction(self.closes, idx)
        if cons_dir >= 5:
            score += 3
            details.append(f'连续{cons_dir}日上行')
        elif cons_dir <= -5:
            score -= 3
            details.append(f'连续{-cons_dir}日下行')

        # MACD状态
        if idx > 0 and self._dif[idx] is not None and self._dea[idx] is not None:
            dif_now = self._dif[idx]
            dea_now = self._dea[idx]

            if dif_now > 0 and dif_now > dea_now:
                score += 2
                details.append('MACD多头运行')
            elif dif_now < 0 and dif_now < dea_now:
                score -= 2
                details.append('MACD空头运行')

        return DimensionResult('momentum', max(-10, min(10, score)), 0.25, details)

    # ─── 维度3: 成交量能量 (权重20%) ───
    def _judge_volume_energy(self, idx: int) -> DimensionResult:
        details = []
        score = 0.0

        vol = self.vols[idx]

        if idx < 20 or self._vol_ma20[idx] is None or self._vol_ma20[idx] <= 0:
            return DimensionResult('volume_energy', 0, 0.20, ['成交量数据不足'])

        vol_ma20_val = self._vol_ma20[idx]
        vol_ratio = vol / vol_ma20_val if vol_ma20_val > 0 else 1.0

        # 量比
        if vol_ratio > 2.0:
            score += 5
            details.append(f'大幅放量(量比={vol_ratio:.1f})')
        elif vol_ratio > 1.5:
            score += 3
            details.append(f'放量(量比={vol_ratio:.1f})')
        elif vol_ratio > 1.0:
            score += 1
            details.append(f'正常偏多(量比={vol_ratio:.1f})')
        elif vol_ratio < 0.5:
            score -= 4
            details.append(f'极致缩量(量比={vol_ratio:.1f})')
        elif vol_ratio < 0.7:
            score -= 2
            details.append(f'缩量(量比={vol_ratio:.1f})')

        # 量价配合度
        if idx > 0 and self.closes[idx - 1] > 0:
            price_change = (self.closes[idx] - self.closes[idx - 1]) / self.closes[idx - 1]
            if price_change > 0.01 and vol_ratio > 1.2:
                score += 3
                details.append('价涨量增(健康)')
            elif price_change > 0.01 and vol_ratio < 0.8:
                score -= 3
                details.append('价涨量缩(背离)')
            elif price_change < -0.01 and vol_ratio > 1.3:
                score -= 4
                details.append('价跌量增(恐慌)')
            elif price_change < -0.01 and vol_ratio < 0.8:
                score += 1
                details.append('价跌量缩(正常调整)')

        # 连续放量
        cons_high_vol = 0
        for j in range(idx, max(0, idx - 10), -1):
            if j >= 20 and self._vol_ma20[j] and self._vol_ma20[j] > 0:
                if self.vols[j] / self._vol_ma20[j] > 1.2:
                    cons_high_vol += 1
                else:
                    break
        if cons_high_vol >= 3:
            score += 4
            details.append(f'连续{cons_high_vol}日放量(主力进攻信号)')

        return DimensionResult('volume_energy', max(-10, min(10, score)), 0.20, details)

    # ─── 维度4: 波动率环境 (权重15%) ───
    def _judge_volatility(self, idx: int) -> DimensionResult:
        details = []
        score = 0.0

        if self.atr14[idx] is None or self.closes[idx] <= 0:
            return DimensionResult('volatility', 0, 0.15, ['ATR数据不足'])

        atr_pct = self.atr14[idx] / self.closes[idx]

        # ATR% 评分
        if atr_pct < 0.015:
            score += 5
            details.append(f'极低波环境(ATR%={atr_pct:.2%})')
        elif atr_pct < 0.025:
            score += 3
            details.append(f'低波环境(ATR%={atr_pct:.2%})')
        elif atr_pct < 0.035:
            score += 1
            details.append(f'正常波动(ATR%={atr_pct:.2%})')
        elif atr_pct > 0.05:
            score -= 5
            details.append(f'高波环境(ATR%={atr_pct:.2%})')
        elif atr_pct > 0.035:
            score -= 2
            details.append(f'偏高波动(ATR%={atr_pct:.2%})')

        # 布林带宽度
        if self._bb_width[idx] is not None:
            bbw = self._bb_width[idx]
            if bbw > 0.15:
                score -= 3
                details.append(f'布林带扩张(BBW={bbw:.1%},趋势加速)')
            elif bbw < 0.05:
                score += 2
                details.append(f'布林带收窄(BBW={bbw:.1%},即将变盘)')

        # 极端涨跌频率
        extreme_count = 0
        start = max(0, idx - 19)
        for j in range(start, idx + 1):
            if self.bars[j].change_pct and abs(self.bars[j].change_pct) > 0.03:
                extreme_count += 1

        if extreme_count > 6:
            score -= 5
            details.append(f'近20日{extreme_count}次极端波动(动荡)')
        elif extreme_count > 4:
            score -= 2
            details.append(f'近20日{extreme_count}次极端波动')
        elif extreme_count <= 1:
            score += 2
            details.append(f'近20日仅{extreme_count}次极端波动(稳定)')

        return DimensionResult('volatility', max(-10, min(10, score)), 0.15, details)

    # ─── 维度5: 市场宽度 (权重15%) ───
    def _judge_market_breadth(self, idx: int, mkt_data: Optional[Dict] = None) -> DimensionResult:
        """
        市场宽度判定。
        需要外部传入 market_breadth_mapped 数据，否则基于均线方向推断。
        """
        details = []
        score = 0.0

        if mkt_data and 'pct_above_ma20' in mkt_data:
            pct = mkt_data['pct_above_ma20']
            if pct > 0.65:
                score += 7
                details.append(f'MA20之上占比{pct:.0%}(强牛市)')
            elif pct > 0.50:
                score += 4
                details.append(f'MA20之上占比{pct:.0%}(偏多)')
            elif pct > 0.35:
                score += 1
                details.append(f'MA20之上占比{pct:.0%}(中性偏多)')
            elif pct > 0.25:
                score -= 1
                details.append(f'MA20之上占比{pct:.0%}(偏空)')
            else:
                score -= 5
                details.append(f'MA20之上占比{pct:.0%}(熊市)')

        if mkt_data and 'new_high_low_ratio' in mkt_data:
            ratio = mkt_data['new_high_low_ratio']
            if ratio > 3:
                score += 3
                details.append(f'新高/新低比={ratio:.1f}(强势)')
            elif ratio < 0.5:
                score -= 4
                details.append(f'新高/新低比={ratio:.1f}(弱势)')

        if not mkt_data:
            # 无市场宽度数据时，用指数自身动量替代
            if self.roc20[idx] is not None and self.roc20[idx] > 0.05:
                score += 3
                details.append('(无宽度数据,用动量替代)动量强势')
            elif self.roc20[idx] is not None and self.roc20[idx] < -0.05:
                score -= 3
                details.append('(无宽度数据,用动量替代)动量弱势')

        return DimensionResult('market_breadth', max(-10, min(10, score)), 0.15, details)

    # ─── 维度6: 趋势持续性 (权重5%) ───
    def _judge_trend_persistence(self, idx: int) -> DimensionResult:
        details = []
        score = 0.0

        if self.ma20[idx] is None:
            return DimensionResult('trend_persistence', 0, 0.05, ['数据不足'])

        close = self.closes[idx]

        # 站在MA20上方的天数
        above_ma20_days = 0
        for j in range(idx, max(0, idx - 20), -1):
            if self.ma20[j] and self.closes[j] > self.ma20[j]:
                above_ma20_days += 1
            else:
                break

        if above_ma20_days >= 10:
            score += 4
            details.append(f'连续{above_ma20_days}日站稳MA20(趋势牢固)')
        elif above_ma20_days >= 5:
            score += 2
            details.append(f'连续{above_ma20_days}日站稳MA20')
        else:
            below_days = 0
            for j in range(idx, max(0, idx - 20), -1):
                if self.ma20[j] and self.closes[j] < self.ma20[j]:
                    below_days += 1
                else:
                    break
            if below_days >= 10:
                score -= 3
                details.append(f'连续{below_days}日在MA20下方(趋势疲弱)')

        # RSI极端
        if self.rsi14[idx] is not None:
            rsi = self.rsi14[idx]
            if rsi > 75:
                score -= 2
                details.append(f'RSI过热({rsi:.0f})')
            elif rsi < 25:
                score += 2
                details.append(f'RSI超卖({rsi:.0f})')

        return DimensionResult('trend_persistence', max(-10, min(10, score)), 0.05, details)

    # ─── 辅助函数 ───
    @staticmethod
    def _slope_sign(current: float, prev: Optional[float]) -> int:
        if prev is None or prev == 0:
            return 0
        if current > prev * 1.001:
            return 1
        elif current < prev * 0.999:
            return -1
        return 0

    @staticmethod
    def _safe_ma(ma_list: List[float], idx: int, offset: int) -> Optional[float]:
        target = idx + offset
        if 0 <= target < len(ma_list):
            return ma_list[target]
        return None

    @staticmethod
    def _score_to_season(raw_score: float) -> str:
        if raw_score > 5.5:
            return 'spring'
        elif raw_score > 2.5:
            return 'summer'
        elif raw_score > -3.0:
            return 'chaos'
        elif raw_score > -5.5:
            return 'autumn'
        else:
            return 'winter'

    @staticmethod
    def _score_to_season_adjusted(raw_score: float, regime: str) -> str:
        """v2.2: 赛季细化 — 混沌拆分为弱春/弱秋子态"""
        if regime == 'bull':
            if raw_score > 6.0: return 'spring'
            elif raw_score > 3.0: return 'summer'
            elif raw_score > 1.5: return 'chaos_spring'
            elif raw_score > -1.5: return 'chaos'
            elif raw_score > -3.5: return 'chaos_autumn'
            elif raw_score > -5.0: return 'autumn'
            else: return 'winter'
        elif regime == 'bear':
            if raw_score > 5.0: return 'spring'
            elif raw_score > 2.0: return 'summer'
            elif raw_score > 0.5: return 'chaos_spring'
            elif raw_score > -1.5: return 'chaos'
            elif raw_score > -3.5: return 'chaos_autumn'
            elif raw_score > -4.5: return 'autumn'
            else: return 'winter'
        else:  # range
            if raw_score > 5.5: return 'spring'
            elif raw_score > 2.5: return 'summer'
            elif raw_score > 1.0: return 'chaos_spring'
            elif raw_score > -2.0: return 'chaos'
            elif raw_score > -3.5: return 'chaos_autumn'
            elif raw_score > -5.5: return 'autumn'
            else: return 'winter'

    @staticmethod
    def _get_scoring_strategy(season: str, regime: str, chaos_subtype: str = None) -> str:
        """
        评分策略: momentum(追强) / reversion(买跌)
        
        P6 分季评分双轨制 — MAY方案定版 (2026-06-01):
        --------------------------------------------------
        | 季节 | 混沌子态 | regime | 轨道 |
        |------|---------|--------|------|
        | autumn/winter | 任意 | 任意 | reversion |
        | chaos | chaos_bearish | 任意 | reversion |
        | chaos | chaos_neutral | bear | reversion |
        | chaos | chaos_bullish | 任意 | momentum |
        | chaos | chaos_neutral | bull/range | momentum |
        | spring/summer | 任意 | 任意 | momentum |
        --------------------------------------------------
        设计者: MAY
        """
        # 真秋/冬 → 无条件回归
        if season in ('autumn', 'winter'):
            return 'reversion'
        # 偏空混沌 → 回归 (散点空头占优, 均值回归因子捕获超跌)
        if season == 'chaos' and chaos_subtype == 'chaos_bearish':
            return 'reversion'
        # 中性混沌 × 熊市 → 回归 (熊市不赌方向)
        if season == 'chaos' and chaos_subtype == 'chaos_neutral' and regime == 'bear':
            return 'reversion'
        # 其余(偏多混沌、中性混沌+非熊市、春/夏) → 动量
        return 'momentum'

    @staticmethod
    def _calc_confidence(scores: List[float]) -> float:
        """置信度 = 1 - 标准差/10, 越一致越高"""
        n = len(scores)
        if n == 0:
            return 0.0
        mean = sum(scores) / n
        variance = sum((s - mean) ** 2 for s in scores) / n
        std = math.sqrt(variance)
        confidence = 1.0 - (std / 10.0)
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _build_rule_chain(dimensions: List[DimensionResult], season: str,
                          raw_score: float, confidence: float, regime: str = 'range',
                          chaos_subtype: str = None, scoring_strategy: str = 'momentum') -> str:
        """构建可解释的规则链 (v2.1: 含三态+策略)"""
        parts = [f"总分={raw_score:+.1f} → {SEASON_MAP.get(season, season)}(置信度={confidence:.0%})"]
        parts.append(f"市场状态: {REGIME_MAP.get(regime, regime)} | 评分策略: {scoring_strategy}")
        if chaos_subtype:
            cmap = {'chaos_bullish': '偏多混沌', 'chaos_bearish': '偏空混沌', 'chaos_neutral': '中性混沌'}
            parts.append(f"混沌细分: {cmap.get(chaos_subtype, chaos_subtype)}")
        parts.append("维度拆解:")
        for d in sorted(dimensions, key=lambda x: abs(x.score), reverse=True):
            parts.append(f"  [{d.name}]({d.weight:.0%})得分={d.score:+.1f}: {'; '.join(d.details)}")
        return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 市场宽度计算器 (从个股数据聚合)
# ═══════════════════════════════════════════════════════════════

class MarketBreadthCalculator:
    """从个股数据计算市场宽度指标"""

    def __init__(self, db_config: dict = None):
        self.db_config = db_config

    def compute_above_ma20_pct(self, stock_data: Dict[str, List[KLineBar]],
                                target_date: date) -> Optional[float]:
        """计算MA20之上的股票占比"""
        above = 0
        total = 0
        for ts_code, bars in stock_data.items():
            # 找到target_date对应位置
            closes = [b.close for b in bars]
            ma20 = TechIndicators.sma(closes, 20)
            for i in range(len(bars) - 1, -1, -1):
                if bars[i].trade_date <= target_date:
                    if ma20[i] and bars[i].close > ma20[i]:
                        above += 1
                    total += 1
                    break
        return above / total if total > 0 else None

    def compute_new_high_low_ratio(self, stock_data: Dict[str, List[KLineBar]],
                                    target_date: date, lookback: int = 20) -> Optional[float]:
        """计算20日新高/新低数量比"""
        new_highs = 0
        new_lows = 0
        for ts_code, bars in stock_data.items():
            if len(bars) < lookback:
                continue
            # 找到target_date位置
            idx = None
            for i in range(len(bars) - 1, -1, -1):
                if bars[i].trade_date <= target_date:
                    idx = i
                    break
            if idx is None or idx < lookback:
                continue

            window_high = max(b.high for b in bars[idx - lookback:idx + 1])
            window_low = min(b.low for b in bars[idx - lookback:idx + 1])

            current_close = bars[idx].close
            if current_close >= window_high * 0.98:  # 接近新高
                new_highs += 1
            if current_close <= window_low * 1.02:  # 接近新低
                new_lows += 1

        if new_lows == 0:
            return 5.0 if new_highs > 0 else 1.0
        return new_highs / new_lows

    def get_breadth_for_date(self, loader: DataLoader, target_date: date) -> Dict:
        """获取指定日期的市场宽度数据"""
        codes = loader.get_stock_pool_codes()
        if not codes:
            return {}

        stock_data = loader.load_stock_kline_history(codes)

        result = {}
        pct = self.compute_above_ma20_pct(stock_data, target_date)
        if pct is not None:
            result['pct_above_ma20'] = round(pct, 4)

        ratio = self.compute_new_high_low_ratio(stock_data, target_date)
        if ratio is not None:
            result['new_high_low_ratio'] = round(ratio, 2)

        return result


# ═══════════════════════════════════════════════════════════════
# 多指数综合判定引擎
# ═══════════════════════════════════════════════════════════════

class SeasonEngine:
    """
    恒纪元四季判定引擎主类

    使用方法:
        engine = SeasonEngine()
        result = engine.judge_market_season()  # 最新交易日
        result = engine.get_realtime_season()   # 同上，alias
    """

    def __init__(self, db_config: dict = None, use_market_breadth: bool = True):
        import os
        pwd = os.environ.get('MYSQL_PASS', '')
        if not pwd:
            pwd = 'iXve1rVBXfdA4tL9'
        self.db_config = db_config if db_config else {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':pwd,'database':'stock_db_v2'}
        self.use_market_breadth = use_market_breadth
        self.loader = DataLoader()
        self._prev_seasons: Dict[str, str] = {}  # 上一季的记忆(用于防横跳)
        self._season_change_dates: Dict[str, date] = {}  # 季节切换日期

    def close(self):
        self.loader.close()

    def judge_market_season(self, target_date: Optional[date] = None) -> Dict:
        """
        对5个指数进行综合四季判定

        Args:
            target_date: 目标日期，None=最新交易日

        Returns:
            {
                'trade_date': '2026-05-25',
                'market_season': 'summer',
                'market_confidence': 0.85,
                'raw_score': 3.2,
                'index_details': {...},
                'rule_chain': '...',
                'position_advice': '...',
            }
        """
        # 连接修改
        self.loader.conn = pymysql.connect(**self.db_config)

        index_results = {}
        all_scores = []
        all_confidences = []

        # 市场宽度(批量计算一次，缓存)
        breadth_cache = {}
        if self.use_market_breadth:
            breadth_calc = MarketBreadthCalculator(self.db_config)
            # 先确定日期
            if target_date is None:
                cur = self.loader.conn.cursor()
                # 从监控池个股K线取最新交易日（指数数据Tushare免费账户可能不可用）
                cur.execute("SELECT MAX(trade_date) FROM daily_kline")
                target_date = cur.fetchone()[0]
                cur.close()

        for ts_code, cfg in INDEX_CONFIG.items():
            bars = self.loader.load_index_kline(ts_code)
            if len(bars) < 130:
                index_results[ts_code] = {
                    'name': cfg['name'],
                    'weight': cfg['weight'],
                    'role': cfg['role'],
                    'season': 'chaos',
                    'confidence': 0,
                    'error': f'数据不足({len(bars)}条)',
                }
                continue

            # 找到目标日期在bars中的索引
            if target_date:
                idx = None
                for i in range(len(bars) - 1, -1, -1):
                    if bars[i].trade_date <= target_date:
                        idx = i
                        break
                if idx is None:
                    idx = len(bars) - 1
            else:
                idx = len(bars) - 1

            # 市场宽度数据
            breadth_data = None
            if self.use_market_breadth and ts_code == '000300.SH':
                # 市场宽度用主指数(沪深300)的日期计算一次
                bdate = bars[idx].trade_date
                if bdate not in breadth_cache:
                    breadth_cache[bdate] = breadth_calc.get_breadth_for_date(self.loader, bdate)
                breadth_data = breadth_cache[bdate]

            # 执行判定
            judge = SeasonJudge(bars)
            season, confidence, details = judge.judge_at(idx, breadth_data)

            # 防横跳逻辑
            season = self._apply_season_smoothing(ts_code, bars[idx].trade_date, season)

            all_scores.append(details['raw_score'])
            all_confidences.append(confidence)

            index_results[ts_code] = {
                'name': cfg['name'],
                'weight': cfg['weight'],
                'role': cfg['role'],
                'season': season,
                'confidence': confidence,
                'raw_score': details['raw_score'],
                'regime': details.get('regime', 'range'),
                'scoring_strategy': details.get('scoring_strategy', 'reversion'),
                'dimensions': details['dimensions'],
                'rule_chain': details['rule_chain'],
                'close': bars[idx].close,
            }

        # ─── v2.1: 综合三态(取沪深300的三态作为市场状态基准) ───
        market_regime = index_results.get('000300.SH', {}).get('regime', 'range')
        market_scoring = index_results.get('000300.SH', {}).get('scoring_strategy', 'reversion')

        # ─── 加权综合 ───
        # 方法: 加权投票制
        season_votes: Dict[str, float] = defaultdict(float)
        for ts_code, cfg in INDEX_CONFIG.items():
            if ts_code in index_results and 'error' not in index_results[ts_code]:
                s = index_results[ts_code]['season']
                season_votes[s] += cfg['weight']

        if season_votes:
            market_season = max(season_votes, key=season_votes.get)
        else:
            market_season = 'chaos'

        # 综合置信度: 加权平均
        weighted_conf = 0.0
        total_w = 0.0
        for ts_code, cfg in INDEX_CONFIG.items():
            if ts_code in index_results and 'error' not in index_results[ts_code]:
                weighted_conf += index_results[ts_code]['confidence'] * cfg['weight']
                total_w += cfg['weight']
        market_confidence = weighted_conf / total_w if total_w > 0 else 0.0

        # 综合raw_score
        market_raw = sum(all_scores) / len(all_scores) if all_scores else 0.0

        # 构建rule_chain
        vote_parts = []
        for s, w in sorted(season_votes.items(), key=lambda x: -x[1]):
            vote_parts.append(f"{SEASON_MAP.get(s,s)}: {w:.0%}")
        rule_chain = f"加权投票 → {SEASON_MAP.get(market_season, market_season)}\n"
        rule_chain += f"票数分布: {' | '.join(vote_parts)}\n"
        rule_chain += f"综合得分: {market_raw:+.1f} | 置信度: {market_confidence:.0%}"

        # 仓位建议
        position = self._get_position_advice(market_season, market_confidence)

        # 恒纪元等级/评分（使用共享函数推断）
        hj_level, hj_score = infer_hengjiyuan(market_season, market_raw)

        return {
            'trade_date': target_date.isoformat() if hasattr(target_date, 'isoformat') else str(target_date),
            'market_season': market_season,
            'market_confidence': round(market_confidence, 3),
            'raw_score': round(market_raw, 2),
            'market_regime': market_regime,
            'market_scoring_strategy': market_scoring,
            'index_details': index_results,
            'season_votes': {s: round(v, 3) for s, v in season_votes.items()},
            'rule_chain': rule_chain,
            'position_advice': position,
            'hengjiyuan_level': hj_level,
            'hengjiyuan_score': hj_score,
        }

    def get_realtime_season(self) -> Dict:
        """获取最新交易日的市场季节(实时判定)"""
        return self.judge_market_season(target_date=None)

    def judge_history(self, start_date: Optional[date] = None,
                      end_date: Optional[date] = None) -> List[Dict]:
        """
        对历史区间逐日判定(用于回测)
        """
        # 加载所有指数的完整数据
        index_data: Dict[str, List[KLineBar]] = {}
        for ts_code in INDEX_CONFIG:
            bars = self.loader.load_index_kline(ts_code)
            if bars:
                index_data[ts_code] = bars

        if not index_data:
            return []

        # 确定日期范围
        all_dates = set()
        for bars in index_data.values():
            for b in bars:
                all_dates.add(b.trade_date)
        all_dates = sorted(all_dates)

        # 过滤范围（仅取MA120有效之后的数据）
        min_valid_dates = []
        for ts_code, bars in index_data.items():
            closes = [b.close for b in bars]
            ma120 = TechIndicators.sma(closes, 120)
            # 找到第一个有效的日期
            for i, m in enumerate(ma120):
                if m is not None:
                    min_valid_dates.append(bars[i].trade_date)
                    break
        effective_start = max(min_valid_dates) if min_valid_dates else all_dates[0]

        if start_date and start_date > effective_start:
            effective_start = start_date
        if end_date is None:
            end_date = all_dates[-1]

        results = []

        # 批量计算市场宽度(如果需要)
        breadth_calc = MarketBreadthCalculator(self.db_config) if self.use_market_breadth else None

        for d in all_dates:
            if d < effective_start or d > end_date:
                continue

            day_results = {'trade_date': d}
            all_scores = []
            season_votes: Dict[str, float] = defaultdict(float)

            # 市场宽度(只算一次)
            breadth_data = None
            if breadth_calc:
                breadth_data = breadth_calc.get_breadth_for_date(self.loader, d)

            first_index = True
            for ts_code, cfg in INDEX_CONFIG.items():
                if ts_code not in index_data:
                    continue
                bars = index_data[ts_code]

                # 找到该日期的位置
                idx = None
                for i in range(len(bars) - 1, -1, -1):
                    if bars[i].trade_date == d:
                        idx = i
                        break
                if idx is None:
                    continue

                judge = SeasonJudge(bars)
                # 市场宽度只给主指数用
                bd = breadth_data if first_index else None
                first_index = False
                season, conf, details = judge.judge_at(idx, bd)

                # 防横跳
                season = self._apply_season_smoothing(ts_code, d, season)

                all_scores.append(details['raw_score'])
                season_votes[season] += cfg['weight']

            if season_votes:
                market_season = max(season_votes, key=season_votes.get)
                market_raw = sum(all_scores) / len(all_scores) if all_scores else 0

                # v2.1: 提取沪深300的regime和scoring
                regime = details.get('regime', 'range')
                scoring = details.get('scoring_strategy', 'reversion')

                results.append({
                    'trade_date': d,
                    'season': market_season,
                    'raw_score': round(market_raw, 2),
                    'regime': regime,
                    'scoring_strategy': scoring,
                    'votes': {s: round(v, 3) for s, v in season_votes.items()},
                })

        return results

    # 连续趋势跟踪(用于秋季/春季确认)
    _consecutive_scores: Dict[str, List[float]] = {}

    def _apply_season_smoothing(self, ts_code: str, trade_date: date, new_season: str) -> str:
        """防止季节反复横跳: 切换后至少保持2个交易日, 且需要连续确认"""
        prev = self._prev_seasons.get(ts_code)
        if prev is None:
            self._prev_seasons[ts_code] = new_season
            self._season_change_dates[ts_code] = trade_date
            return new_season

        if prev == new_season:
            # 记录连续天数 (用于秋/冬确认)
            if ts_code not in self._consecutive_scores:
                self._consecutive_scores[ts_code] = []
            return new_season

        # 季节变化: 检查上次切换时间
        last_change = self._season_change_dates.get(ts_code)
        if last_change and (trade_date - last_change).days < 3:
            # 不到3个自然日, 保持原季节
            return prev

        # 春季和秋季需要额外确认: 混沌→春秋至少需要1天缓冲
        if prev == 'chaos' and new_season in ('spring', 'autumn'):
            if last_change and (trade_date - last_change).days < 2:
                return 'chaos'  # 暂时保持混沌

        # 允许切换
        self._prev_seasons[ts_code] = new_season
        self._season_change_dates[ts_code] = trade_date
        return new_season

    @staticmethod
    def _get_position_advice(season: str, confidence: float) -> str:
        """仓位建议"""
        if season == 'spring':
            return f"进攻期 → 仓位80-100% (置信度{confidence:.0%})"
        elif season == 'summer':
            return f"持有期 → 仓位50-80% (置信度{confidence:.0%})"
        elif season == 'autumn':
            return f"防守期 → 仓位20-30% (置信度{confidence:.0%})"
        elif season == 'winter':
            return f"休眠期 → 仓位0-10%或空仓 (置信度{confidence:.0%})"
        else:
            return f"观望期 → 仓位≤30%, 等待方向 (置信度{confidence:.0%})"


# ═══════════════════════════════════════════════════════════════
# 数据库持久化
# ═══════════════════════════════════════════════════════════════

def infer_hengjiyuan(season: str, raw_score: float) -> tuple:
    """
    根据季节和原始评分推断恒纪元等级和评分
    用于 season_engine 返回结果和 manager_server 各接口的恒纪元数据补齐
    输出: (hengjiyuan_level, hengjiyuan_score)
    """
    if season in ('summer', 'spring'):
        level = 'strong_heng' if raw_score > 2 else 'weak_heng'
    elif season in ('chaos', 'chaos_spring'):
        level = 'weak_heng' if raw_score > 0 else 'weak_luan'
    else:
        level = 'weak_luan' if raw_score < -1 else 'strong_luan'
    score = round(max(0, min(100, (raw_score + 10) * 5)), 1)
    return (level, score)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS season_state (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date DATE NOT NULL COMMENT '交易日期',
    index_code VARCHAR(16) NOT NULL COMMENT '指数代码, MARKET=全市场综合',
    season VARCHAR(20) NOT NULL COMMENT '季节: spring/summer/autumn/winter/chaos',
    raw_score DECIMAL(6,2) COMMENT '原始加权总分(-10~+10)',
    confidence DECIMAL(5,3) COMMENT '置信度(0~1)',
    close_price DECIMAL(12,3) COMMENT '收盘价',
    dimensions_json JSON COMMENT '各维度评分详情JSON',
    rule_chain TEXT COMMENT '可解释规则链',
    position_advice VARCHAR(200) COMMENT '仓位建议',
    season_votes JSON COMMENT '各季节投票权重',
    hengjiyuan_level VARCHAR(20) COMMENT '恒纪元等级 strong_heng/weak_heng/weak_luan/strong_luan',
    hengjiyuan_score DECIMAL(5,2) COMMENT '恒纪元评分(0-100)',
    confidence_mult DECIMAL(5,2) COMMENT '恒纪元置信度系数',
    regime VARCHAR(10) COMMENT '市场状态 bull/bear/range',
    regime_strength DECIMAL(5,3) COMMENT '状态强度',
    chaos_subtype VARCHAR(20) COMMENT '混沌细分',
    scoring_strategy VARCHAR(20) COMMENT '评分策略 momentum/reversion',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_date_code (trade_date, index_code),
    INDEX idx_date (trade_date),
    INDEX idx_season (season),
    INDEX idx_code_season (index_code, season)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='恒纪元四季判定结果表';
"""


def create_table_if_not_exists(db_config: dict = None):
    """创建season_state表"""
    cfg = db_config or {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint'}
    conn = pymysql.connect(**cfg)
    cur = conn.cursor()
    cur.execute(CREATE_TABLE_SQL)
    conn.commit()
    cur.close()
    conn.close()
    print("✅ season_state 表已就绪")


def save_result_to_db(result: Dict, db_config: dict = None):
    """保存单次判定结果到数据库"""
    import os
    if db_config is None:
        pwd = os.environ.get('MYSQL_PASS', '') or 'iXve1rVBXfdA4tL9'
        db_config = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':pwd,'database':'stock_db_v2','charset':'utf8mb4'}
    cfg = db_config
    conn = pymysql.connect(**cfg)
    cur = conn.cursor()

    # 全市场综合
    hj_level, hj_score = (
        (result.get('hengjiyuan_level'), result.get('hengjiyuan_score'))
        if result.get('hengjiyuan_level')
        else infer_hengjiyuan(result['market_season'], result['raw_score'])
    )
    hj_conf = result.get('market_confidence', 0)
    cur.execute("""
        INSERT INTO season_state (trade_date, index_code, season, raw_score, confidence,
                                   rule_chain, position_advice, season_votes,
                                   hengjiyuan_level, hengjiyuan_score, confidence_mult)
        VALUES (%s, 'MARKET', %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE season=VALUES(season), raw_score=VALUES(raw_score),
                                confidence=VALUES(confidence), rule_chain=VALUES(rule_chain),
                                position_advice=VALUES(position_advice),
                                season_votes=VALUES(season_votes),
                                hengjiyuan_level=VALUES(hengjiyuan_level),
                                hengjiyuan_score=VALUES(hengjiyuan_score),
                                confidence_mult=VALUES(confidence_mult)
    """, (
        result['trade_date'],
        result['market_season'],
        result['raw_score'],
        result['market_confidence'],
        result['rule_chain'],
        result['position_advice'],
        json.dumps(result['season_votes'], ensure_ascii=False),
        hj_level,
        hj_score,
        hj_conf,
    ))

    # 各指数明细
    for ts_code, detail in result.get('index_details', {}).items():
        if 'error' in detail:
            continue
        cur.execute("""
            INSERT INTO season_state (trade_date, index_code, season, raw_score, confidence,
                                       close_price, dimensions_json, rule_chain)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE season=VALUES(season), raw_score=VALUES(raw_score),
                                    confidence=VALUES(confidence),
                                    close_price=VALUES(close_price),
                                    dimensions_json=VALUES(dimensions_json),
                                    rule_chain=VALUES(rule_chain)
        """, (
            result['trade_date'],
            ts_code,
            detail['season'],
            detail['raw_score'],
            detail['confidence'],
            detail.get('close'),
            json.dumps(detail.get('dimensions', {}), ensure_ascii=False),
            detail.get('rule_chain', ''),
        ))

    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ 判定结果已写入数据库 ({result['trade_date']})")


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description='恒纪元四季判定引擎 v2.0')
    parser.add_argument('--mode', choices=['now', 'backtest', 'init-db'], default='now',
                        help='now=实时判定, backtest=回测, init-db=初始化数据库表')
    parser.add_argument('--date', type=str, help='指定日期 YYYY-MM-DD')
    parser.add_argument('--no-breadth', action='store_true', help='不使用市场宽度(加速回测)')
    parser.add_argument('--save', action='store_true', help='保存结果到数据库')

    args = parser.parse_args()

    if args.mode == 'init-db':
        create_table_if_not_exists()
        return

    use_breadth = not args.no_breadth
    engine = SeasonEngine(use_market_breadth=use_breadth)

    try:
        if args.mode == 'now':
            target = None
            if args.date:
                target = datetime.strptime(args.date, '%Y-%m-%d').date()
            result = engine.judge_market_season(target)
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

            if args.save:
                save_result_to_db(result)

        elif args.mode == 'backtest':
            print("⏳ 开始历史回测...")
            results = engine.judge_history()
            print(f"✅ 回测完成, 共{len(results)}个交易日")

            # 统计分析
            from collections import Counter
            season_counts = Counter(r['season'] for r in results)
            print("\n📊 季节分布:")
            for s in ['spring', 'summer', 'autumn', 'winter', 'chaos']:
                cnt = season_counts.get(s, 0)
                pct = cnt / len(results) * 100 if results else 0
                print(f"  {SEASON_MAP.get(s, s):20s}: {cnt:4d}天 ({pct:5.1f}%)")

            # 输出最近30天
            print("\n📅 最近30个交易日:")
            for r in results[-30:]:
                d = r['trade_date']
                s = r['season']
                raw = r['raw_score']
                print(f"  {d} | {SEASON_MAP.get(s, s):20s} | 得分={raw:+.1f}")

            # 保存到数据库
            if args.save:
                total = len(results)
                for i, r in enumerate(results):
                    save_result_to_db(r)
                    if (i + 1) % 100 == 0:
                        print(f"  💾 已保存 {i+1}/{total}")
                print(f"✅ 回测结果已全部存入数据库")

    finally:
        engine.close()


if __name__ == '__main__':
    main()
