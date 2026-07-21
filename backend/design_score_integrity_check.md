# 评分管道守护自检 (P0.4) — 接口规范 v1

## 目标
在评分引擎出口自动校验评分的完整性与合理性，第一时间发现异常（如惩罚分恒0、值域越界、分布异常）。

## 接口

### 1. validate_score_integrity()

```python
def validate_score_integrity(
    results: List[Dict],  # score_stock() 返回的完整结果列表
    engine_version: str,  # 引擎版本标识
    config: Dict          # 当前运行配置
) -> Dict:
    """
    评分完整性校验。
    
    返回:
    {
        "status": "pass" | "warn" | "fail",
        "checks": {
            "conservation": {"pass": True, "detail": "..."},
            "penalty_effectiveness": {"pass": True, "detail": "..."},
            "value_range": {"pass": True, "detail": "..."},
            "distribution": {"pass": True, "detail": "..."},
            "contradiction": {"pass": True, "detail": "..."},
        },
        "details": {
            "timestamp": "2026-07-21T20:05:00",
            "total_stocks": 291,
            "engine_version": engine_version,
            "violations": [...]  # 具体违反项
        }
    }
    """
    pass
```

### 2. 校验规则

| 规则 | 条件 | 失败级别 |
|:-----|:-----|:--------:|
| **守恒** | 基础分 + 惩罚分 - 风控扣分 ≈ 最终分 (容差±1) | fail |
| **惩罚有效性** | 惩罚分均值连续N日<0.5 × 非零比例<20% | warn |
| **值域白名单** | 任何最终分不在[0, 100] 或 惩罚分不在[-100, 0] | fail |
| **分布均态** | 所有评分集中在窄区间 (<10分跨度) 或 全部<10或全部>90 | warn |
| **矛盾** | 惩罚分>0但reason为空 \| 风控扣分≠0但level='normal' | warn |

### 3. 告警表 `score_health`

```sql
CREATE TABLE IF NOT EXISTS score_health (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date DATE NOT NULL,
    engine_version VARCHAR(20) NOT NULL,
    total_stocks INT,
    status VARCHAR(10),        -- pass/warn/fail
    penalty_mean DECIMAL(6,2),
    penalty_nonzero_pct DECIMAL(5,2),
    score_mean DECIMAL(6,2),
    score_std DECIMAL(6,2),
    violations JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_date (trade_date)
);
```

### 4. 集成位置

在 `p6_dual_track_engine.py` 的 `score_stock()` 出口：

```python
# 在 score_stock() 末尾，return 之前插入
if results:
    integrity = validate_score_integrity(results, "v13.3e", config)
    if integrity["status"] == "fail":
        log.error(f"[SCORE_INTEGRITY_FAIL] {integrity['details']}")
        # 写入 score_health 表 + 推送告警
    elif integrity["status"] == "warn":
        log.warning(f"[SCORE_INTEGRITY_WARN] {integrity['details']}")
```

## 与现有架构的关系
- 不修改任何评分逻辑
- 只在评分引擎外层加一个包装校验
- 可与 penalty_log 表配合（通过 trade_date 关联）
