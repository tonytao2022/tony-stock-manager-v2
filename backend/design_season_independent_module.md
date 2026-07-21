# 季节判定独立模块 — 接口规范 v1 (2026-07-21)

## 原则
- 评分引擎不直接做季节判定，只通过 context 接口获取
- 季节判定结果可独立验证、独立回测
- 当前 `season_state` 表结构无需修改

## 1. 架构调整

### 当前（单层耦合）
```
score_stock() → SeasonEngine(season_engine.py → season_state表) → 评分计算
```

### 目标（独立服务接口）
```
season_state表 → SeasonResolutionService(独立) → MarketContext.get_effective_season()
                                                            ↓
                                               评分引擎只消费，不生产
```

## 2. SeasonResolutionService 接口

```python
class SeasonResolutionService:
    """
    季节解析服务。
    职责：从 season_state 表读取原始季节数据 → 置信度加权 → 子态回退 → 输出最终季节。
    """
    
    def __init__(self, db_conn=None):
        self._cache: Dict[str, Dict] = {}
        self._last_refresh = None
    
    def refresh(self) -> int:
        """从 season_state 表重新加载最近 N 天数据。返回加载条数。"""
        pass
    
    def get_season_for(self, trade_date: Union[str, date]) -> Dict:
        """
        返回指定交易日的最终季节判定结果。
        
        返回:
        {
            "season": "chaos_spring",
            "regime": "chaos",
            "confidence": 0.65,
            "buy_line_override": 5,       # 低置信时买入线偏移
            "raw_votes": { ... },         # 原始投票数据
            "resolution_chain": [ ... ],  # 解析链路（可追溯）
        }
        """
        pass
    
    def get_buy_line(self, base_line: int, trade_date=None) -> int:
        """返回买入线（base_line + confidence调整）。"""
        pass
    
    def get_latest(self) -> Dict:
        """返回最新交易日季节。"""
        pass
```

## 3. 与现有代码的关系

目前 MarketContext 已经在 p6_dual_track_engine.py 内有 `get_effective_season()` 和 `get_buy_line_override()` 方法（MAY刚刚加的 P1-6）。

**这次剥离不改逻辑**，只是：
1. 新建 `engine/season_resolution.py` 实现 SeasonResolutionService
2. MarketContext 内部调用 SeasonResolutionService 替代直接处理逻辑
3. 现有 season_engine.py 保持不变（作为低级判定引擎）

## 4. 集成方式

```python
# p6_dual_track_engine.py 中 MarketContext.__init__() 增加一行
self._season_resolver = SeasonResolutionService()

# get_effective_season() 改为委托
def get_effective_season(self):
    result = self._season_resolver.get_season_for(self.trade_date)
    return result["season"]
```

## 5. 与赛季表结构兼容

当前 season_state 表的字段全支持。新模块不做表结构变更。
