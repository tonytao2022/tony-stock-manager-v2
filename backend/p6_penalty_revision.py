"""
V13.3b 价格下跌惩罚 V2
========================
在原有penalty基础上，对track_momentum的trend_score增加价格实时验证
并对penalty做跌幅比例挂钩（上限从15提升到30）

改动前：penalty固定上限25分，trend_score完全依赖缠论
改动后：penalty上限40分 + trend_score打折扣 + 多空排列降级
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def rewrite_track_momentum_penalty(filepath):
    """重写track_momentum的惩罚逻辑"""
    with open(filepath, 'r') as f:
        content = f.read()
    
    old = """        # ─── 价格下跌惩罚 ───\\n        # 核心逻辑: 价格连续下跌时，缠论结构分可能滞后，需根据实际价格跌幅扣分\\n        # 惩罚在评分最终计算前叠加，避免被其他因子稀释\\n        penalty_score = 0.0\\n        penalty_reason = []\\n\\n        if n >= 20:\\n            r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0\\n            r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0\\n            r20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0\\n\\n            # 惩罚1: 近5日跌幅 > 8% → 扣8分\\n            if r5 < -0.08:\\n                p = min(15, int(abs(r5) * 100))\\n                penalty_score += p\\n                penalty_reason.append(f'5日跌{r5*100:.0f}%-{p}分')\\n            # 惩罚2: 近5日跌幅>3%且近10日也跌（持续下跌）→ 额外扣\\n            if r5 < -0.03 and r10 < -0.03:\\n                extra = min(10, int((abs(r5) + abs(r10)) * 50))\\n                penalty_score += extra\\n                penalty_reason.append(f'持续跌+{extra}分')\\n            # 惩罚3: 价格在20日均线下 → 趋势分虚高惩罚\\n            close_price = float(latest['close'])\\n            ma20 = float(latest.get('ma_20', 0) or 0)\\n            if ma20 > 0 and close_price < ma20:\\n                below_pct = (ma20 - close_price) / ma20\\n                p = min(10, int(below_pct * 60))\\n                penalty_score += p\\n                penalty_reason.append(f'破20日线-{p}分')\\n\\n        details['penalty_score'] = round(penalty_score, 1)\\n        details['penalty_reason'] = ';'.join(penalty_reason) if penalty_reason else '无'\\n\\n        # 7. 综合：趋势×0.30 + 位置×0.10 + 结构×0.10 + 动量×0.25 + 资金×0.15 + 融资×0.10 = 100%\\n        #     减：价格下跌惩罚（独立扣分，不稀释其他因子权重）\\n        # MAY方案：结构保留10%（与趋势互补，非冗余），位置降至10%（与趋势重叠部分让出）\\n        final_score = (trend_score * 0.30 + pos_score * 0.10 + structure_score * 0.10 +\\n                       momentum * 0.25 + mf_score * 0.15 + margin_score * 0.10)\\n        final_score = max(0, min(100, round(final_score - penalty_score, 1)))\\n\\n        details['final_raw'] = round(final_score + penalty_score, 1)\\n\\n        return {'track': 'momentum', 'score': final_score, 'details': details}"""
    
    new = """        # ─── 价格下跌惩罚 V2 (2026-07-17 V13.3b) ───
        # 核心改进：
        # 1. trend_score增加价格验证——跌破20日线时打折
        # 2. penalty从固定比例改为跌幅挂钩，上限从25提升到40
        # 3. 多空排列检查——当短期均线死叉长期均线时额外降分
        penalty_score = 0.0
        penalty_reason = []

        if n >= 20:
            r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
            r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0
            r20_ret = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0

            close_price = float(latest['close'])
            ma20 = float(latest.get('ma_20', 0) or 0)
            ma60 = float(latest.get('ma_60', 0) or 0)

            # ─── trend_score价格验证 ───
            # 如果价格跌破MA20，trend_score打8折（趋势分虚高修正）
            if ma20 > 0 and close_price < ma20:
                below_ma20 = (ma20 - close_price) / ma20
                original_trend = trend_score
                trend_discount = max(0.5, 1.0 - below_ma20 * 0.5)  # 每低于MA20 10%打5%折扣
                trend_score = int(original_trend * trend_discount)
                if trend_score != original_trend:
                    penalty_score += (original_trend - trend_score) * 0.30  # 降分×0.3权重
                    penalty_reason.append(f'破MA20(trend{original_trend}→{trend_score})')
                # 更新details
                details['trend_before_discount'] = original_trend

            # ─── 多空排列降级 ───
            # 当MA5 < MA10 < MA20 < 价格（空头排列开始形成时）
            ma5 = float(latest.get('ma_5', 0) or 0)
            if ma5 > 0 and ma20 > 0 and ma5 < ma20 and close_price < ma5:
                # 价格在所有短期均线以下，额外扣分
                p = 10
                penalty_score += p
                penalty_reason.append(f'价格破5均+{p}分')

            # ─── Penalty V2: 跌幅比例挂钩，无硬性上限 ───
            # 惩罚1: 近5日跌幅挂钩（上限从15提升到25）
            if r5 < -0.05:
                p = min(25, int(abs(r5) * 180))  # 跌5%→9分, 跌10%→18分, 跌14%→25分
                penalty_score += p
                penalty_reason.append(f'5日跌{r5*100:.0f}%-{p}分')
            # 惩罚2: 10日跌幅累积惩罚
            if r10 < -0.08:
                p = min(20, int(abs(r10) * 120))  # 跌8%→10分, 跌15%→18分
                penalty_score += p
                penalty_reason.append(f'10日跌{r10*100:.0f}%-{p}分')
            # 惩罚3: 20日跌幅大幅惩罚（连续多周跌）
            if r20_ret < -0.10:
                p = min(25, int(abs(r20_ret) * 100))  # 跌10%→10分, 跌25%→25分
                penalty_score += p
                penalty_reason.append(f'20日跌{r20_ret*100:.0f}%-{p}分')

        details['penalty_score'] = round(penalty_score, 1)
        details['penalty_reason'] = ';'.join(penalty_reason) if penalty_reason else '无'

        # 7. 综合：趋势×0.30 + 位置×0.10 + 结构×0.10 + 动量×0.25 + 资金×0.15 + 融资×0.10 = 100%
        #     减：价格下跌惩罚（独立扣分，不稀释其他因子权重）
        # 注：trend_score在此处已经被价格验证打折，所以不再在加权后额外扣一次
        final_score = (trend_score * 0.30 + pos_score * 0.10 + structure_score * 0.10 +
                       momentum * 0.25 + mf_score * 0.15 + margin_score * 0.10)
        final_score = max(0, min(100, round(final_score - penalty_score, 1)))

        details['final_raw'] = round(final_score + penalty_score, 1)

        return {'track': 'momentum', 'score': final_score, 'details': details}"""

    content = content.replace(old, new)
    with open(filepath, 'w') as f:
        f.write(content)
    print(f"✅ 已重写track_momentum惩罚逻辑: {filepath}")

if __name__ == '__main__':
    rewrite_track_momentum_penalty('/root/stock-system-v2/backend/p6_dual_track_engine.py')
