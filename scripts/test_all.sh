#!/bin/bash
# stock-system-v2 全链路测试
set -e

PASS=0
FAIL=0

check() {
  local desc="$1"
  local cmd="$2"
  if eval "$cmd" 2>/dev/null; then
    echo "  ✅ $desc"
    PASS=$((PASS+1))
  else
    echo "  ❌ $desc"
    FAIL=$((FAIL+1))
  fi
}

echo "════════════════════════════════════════════"
echo "  stock-system-v2 全链路测试"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════════"
echo ""

MYSQL_PASS=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk '{print $3}')

echo "🔥 1. 基础设施"
check "服务运行中" "systemctl is-active --quiet stock-system-v2.service"
check "端口8891监听" "ss -tlnp | grep -q 8891"
check "数据库可达" "mysql -u debian-sys-maint -p$MYSQL_PASS -e 'SELECT 1' &>/dev/null"
check "数据库stock_db_v2存在" "mysql -u debian-sys-maint -p$MYSQL_PASS -e 'USE stock_db_v2; SELECT 1' &>/dev/null"
check "Nginx配置有效" "/usr/sbin/nginx -t &>/dev/null"
check "定时管道已注册" "systemctl is-active --quiet stock-pipeline-v2.timer"

echo ""
echo "🔥 2. 后端API"
TOKEN=*** -s -X POST http://127.0.0.1:8891/api/v2/auth/token -H 'Content-Type: application/json' | python3 -c "
import sys,json
d=json.load(sys.stdin)
if d.get('code')==0 and d.get('data',{}).get('token'):
    open('/tmp/v2_token.txt','w').write(d['data']['token'])
    sys.exit(0)
sys.exit(1)
")
TOKEN=$(cat /tmp/v2_token.txt 2>/dev/null || echo "")

check "健康检查(无认证)" "curl -s http://127.0.0.1:8891/health | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 else exit(1)\""
check "系统健康(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/system/health | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 else exit(1)\""
check "驾驶舱(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/dashboard | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 and len(d.get('data',{}).get('top5',[]))>0 else exit(1)\""
check "策略信号(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/strategy/signals | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 else exit(1)\""
check "持仓列表(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/holdings | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 and len(d.get('data',{}).get('holdings',[]))>=0 else exit(1)\""
check "策略配置(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/strategy/config | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 and len(d.get('data',{}).get('config',{}))>=15 else exit(1)\""
check "板块排行(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/sector/top | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 else exit(1)\""
check "持仓检查点(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/strategy/checkpoints | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 else exit(1)\""
check "持仓盈亏计算(JWT)" "curl -s -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:8891/api/v2/holdings/calc | python3 -c \"import sys,json; d=json.load(sys.stdin); exit(0) if d.get('code')==0 else exit(1)\""

echo ""
echo "🔥 3. 前端页面"
check "驾驶舱页面" "curl -s -o /dev/null -w '' -f http://localhost/stock-v2/index.html"
check "策略管理页面" "curl -s -o /dev/null -w '' -f http://localhost/stock-v2/strategy.html"
check "持仓管理页面" "curl -s -o /dev/null -w '' -f http://localhost/stock-v2/holdings.html"
check "api-key.js" "curl -s -o /dev/null -w '' -f http://localhost/stock-v2/api-key.js"

echo ""
echo "🔥 4. 数据完整性"
check "K线数据 > 70000条" "mysql -u debian-sys-maint -p$MYSQL_PASS -N -e 'SELECT COUNT(*)>70000 FROM stock_db_v2.daily_kline' 2>/dev/null | grep -q 1"
check "评分数据 = 291只" "mysql -u debian-sys-maint -p$MYSQL_PASS -N -e 'SELECT COUNT(*)=291 FROM stock_db_v2.strategy_signal WHERE trade_date=(SELECT MAX(trade_date) FROM stock_db_v2.strategy_signal)' 2>/dev/null | grep -q 1"
check "季节状态存在" "mysql -u debian-sys-maint -p$MYSQL_PASS -N -e 'SELECT COUNT(*)>0 FROM stock_db_v2.season_state' 2>/dev/null | grep -q 1"

echo ""
echo "🔥 5. 独立验证"
check "无旧项目引用" "grep -r '陶的投资预测模型\|openclaw/workspace/projects' /root/stock-system-v2/ --include='*.py' --include='*.sh' --include='*.html' 2>/dev/null | wc -l | grep -q 0"
check "数据库独立(非stock_db)" "mysql -u debian-sys-maint -p$MYSQL_PASS -N -e "SELECT COUNT(*)=0 FROM information_schema.TABLES WHERE TABLE_SCHEMA='stock_db' AND TABLE_NAME='strategy_signal' AND TABLE_ROWS>0;" 2>/dev/null | grep -q 0 && echo "skip" > /dev/null"

echo ""
echo "════════════════════════════════════════════"
echo "  结果: $PASS 通过, $FAIL 失败"
echo "════════════════════════════════════════════"
exit $FAIL
