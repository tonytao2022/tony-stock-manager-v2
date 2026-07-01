#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════
# stock-system-v2 一键部署脚本
# 用法: bash scripts/deploy.sh
# ═══════════════════════════════════════════════════════════════

REPO_DIR="/root/stock-system-v2"
DB_NAME="stock_db_v2"
SERVICE_PORT=8891
FRONTEND_DIR="/var/www/html/stock-v2"
NGINX_CONF="/etc/nginx/sites-enabled/default"

echo "════════════════════════════════════════════"
echo "  stock-system-v2 部署开始"
echo "════════════════════════════════════════════"
echo ""

# 1. 创建虚拟环境
if [ ! -d "$REPO_DIR/venv" ]; then
  echo "📦 创建虚拟环境..."
  python3 -m venv "$REPO_DIR/venv"
  "$REPO_DIR/venv/bin/pip" install flask pymysql requests --quiet
  echo "  ✅ 虚拟环境就绪"
else
  echo "📦 虚拟环境已存在"
fi

# 2. 建数据库
echo "🗄️ 初始化数据库..."
MYSQL_PASS=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk '{print $3}')
mysql -u debian-sys-maint -p$MYSQL_PASS -e "CREATE DATABASE IF NOT EXISTS $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null
# 仅当空库时导入DDL
TABLE_COUNT=*** -N -e "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='$DB_NAME';" 2>/dev/null)
if [ "$TABLE_COUNT" -eq 0 ] || [ "$TABLE_COUNT" = "" ]; then
  mysql -u debian-sys-maint -p$MYSQL_PASS $DB_NAME < "$REPO_DIR/backend/ddl_all.sql" 2>/dev/null
  echo "  ✅ 数据库表已创建 ($TABLE_COUNT → 20张)"
else
  echo "  ✅ 数据库已有 $TABLE_COUNT 张表，跳过DDL"
fi

# 3. 创建前端目录
echo "🌐 创建前端目录..."
mkdir -p "$FRONTEND_DIR"
cp -r "$REPO_DIR/frontend/"* "$FRONTEND_DIR/"
echo "  ✅ 前端文件已复制"

# 4. 注册systemd服务
echo "⚙️ 注册 systemd 服务..."
cp "$REPO_DIR/config/stock-system-v2.service" /etc/systemd/system/
cp "$REPO_DIR/config/stock-pipeline-v2.service" /etc/systemd/system/
cp "$REPO_DIR/config/stock-pipeline-v2.timer" /etc/systemd/system/
systemctl daemon-reload
echo "  ✅ systemd 已注册"

# 5. 配置Nginx
echo "🌐 配置 Nginx..."
if ! grep -q "location /stock-v2/" $NGINX_CONF 2>/dev/null; then
  sed -i '/^server {/a\
    location /stock-v2/ {\
        alias /var/www/html/stock-v2/;\
        try_files $uri $uri/ /stock-v2/index.html;\
        add_header Cache-Control "no-store, no-cache, must-revalidate";\
    }\
\
    location /stock-v2/api/ {\
        proxy_pass http://127.0.0.1:8891/api/;\
        proxy_set_header Host $host;\
        proxy_set_header X-Real-IP $remote_addr;\
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\
        proxy_read_timeout 300s;\
    }' $NGINX_CONF
  /usr/sbin/nginx -t && /usr/sbin/nginx -s reload
  echo "  ✅ Nginx配置已添加"
else
  echo "  ✅ Nginx配置已存在"
fi

# 6. 启动服务
echo "🚀 启动服务..."
systemctl enable stock-system-v2.service 2>/dev/null
systemctl start stock-system-v2.service
sleep 3

if systemctl is-active --quiet stock-system-v2.service; then
  echo "  ✅ 服务运行中 (Port $SERVICE_PORT)"
else
  echo "  ❌ 服务启动失败，检查日志: journalctl -u stock-system-v2.service -n 20"
  exit 1
fi

# 7. 注册定时管道
echo "⏰ 注册定时管道..."
systemctl enable stock-pipeline-v2.timer 2>/dev/null
systemctl start stock-pipeline-v2.timer 2>/dev/null
echo "  ✅ 定时管道已注册 (每日17:00)"

# 8. 健康检查
echo "🏥 健康检查..."
HEALTH=*** -s http://127.0.0.1:$SERVICE_PORT/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('  服务:', d['data']['status'] if d.get('code')==0 else 'ERROR'); print('  数据库:', d['data']['database'] if d.get('code')==0 else 'ERROR')")
echo ""

echo "════════════════════════════════════════════"
echo "  ✅ stock-system-v2 部署完成！"
echo ""
echo "  前端: http://localhost/stock-v2/"
echo "  API:  http://localhost:$SERVICE_PORT/"
echo "  仓库: https://github.com/tonytao2022/stock-system-v2"
echo "════════════════════════════════════════════"
