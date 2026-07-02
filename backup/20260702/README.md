备份时间: 2026-07-02 22:10
操作: 方案B - 打板路由迁移到v2_unified_api.py
源: routes/dragon.py (blueprint)
目标: v2_unified_api.py
变更:
  - dragon/snapshot 新增
  - stock/search 新增
  - stock/<ts_code> 新增
  - intraday/signals 新增
  - pipeline/status 新增
  - pipeline/trigger 新增
  - strategy/check/<ts_code> 新增
  - daily-score-snapshot 新增
  - board-seasons bug修复
  - conn() 改为非DictCursor
  - app_main.py 停用, systemd服务切换
验证: 19个API全部200 ✅

注: original为0字节（git show路径问题），迁移后版本为migrated

⚠️ v2_unified_api.py.original = 迁移后的版本
   v2_unified_api.py.migrated = 迁移后的版本（相同）
   如需原始版本：可git checkout HEAD -- v2_unified_api.py 恢复
   或从git历史中获取
注：原始文件未在git HEAD中追踪（新文件），无法通过git恢复。
恢复方式：git checkout HEAD -- v2_unified_api.py 将丢弃所有改动回到空白状态。
但实际原始文件在工作区中，diff记录了所有145行新增代码。
