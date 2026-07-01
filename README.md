# 📈 股票智能分析管理系统 v2

全新构建，与 v1 完全独立。

## 架构
- 后端：Flask Blueprint 模块化（Port 8891）
- 前端：静态 HTML（/var/www/html/stock-v2/）
- 数据库：MySQL stock_db_v2
- 单管道：每日 17:00 原子步骤

## 启动
```bash
bash scripts/deploy.sh
```
